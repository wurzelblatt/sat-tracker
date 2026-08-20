"""Convert SGP4's TEME output into WGS84 geodetic coordinates.

SGP4 does not return a position on the Earth. It returns a vector in
TEME (True Equator, Mean Equinox), an inertial frame that does not
rotate with the planet. Turning that into a latitude and longitude a map
can draw takes two steps:

    TEME  --rotate by GMST-->  ECEF  --ellipsoid-->  lat / lon / alt

The first step accounts for the Earth having rotated underneath the
inertial frame. The second accounts for the Earth not being a sphere.

── What this module deliberately does not do ────────────────────────
Strictly, the chain is TEME -> PEF -> ITRF, where PEF -> ITRF applies
polar motion: the Earth's rotation axis wanders by a few metres relative
to the crust. That correction needs Earth-orientation parameters
published by the IERS, and it is worth tens of metres at most. It is
skipped, so what this module calls ECEF is really PEF.

For the same reason GMST is computed from UTC rather than UT1. The two
differ by at most 0.9 s, which is up to ~0.4 km of rotation at the
equator. Both approximations are dominated by SGP4's own error, which
grows 1-3 km per day from epoch — at a typical 12-hour epoch age the
element set is already good to only about 1 km.

Getting UT1 would mean ingesting IERS earth-orientation data, which is
recorded as a possible future extension rather than done here.

── Frames and units ─────────────────────────────────────────────────
Positions are in kilometres throughout, as SGP4 returns them.

Angles are in RADIANS inside this module, because that is what `math`
speaks. Latitude and longitude come out in DEGREES, because that is
what `gold.position_snapshot` stores and what every mapping library
expects. The conversion happens once, at the boundary, in
`ecef_to_geodetic`.

── A trap worth knowing about ───────────────────────────────────────
SGP4's gravity model is WGS72; that is baked into the theory and is
what `sgp4.omm.initialize` uses. The ELLIPSOID used below to turn ECEF
into latitude and longitude is WGS84. Those are different things and
must not be unified: using WGS72's ellipsoid here because SGP4 used
WGS72 internally introduces a few hundred metres of error, which is
small enough to look correct.
"""

import numpy as np

# WGS84 reference ellipsoid. Defining constants are the semi-major axis
# and the flattening; everything else is derived from them.
WGS84_A_KM = 6378.137
"""Semi-major axis (equatorial radius) in km."""

WGS84_F = 1 / 298.257223563
"""Flattening. The poles are about 21 km closer to the centre than the equator."""

WGS84_B_KM = WGS84_A_KM * (1 - WGS84_F)
"""Semi-minor axis (polar radius) in km."""

WGS84_E2 = WGS84_F * (2 - WGS84_F)
"""First eccentricity squared, e^2."""

WGS84_EP2 = WGS84_E2 / (1 - WGS84_E2)
"""Second eccentricity squared, e'^2. Used by Bowring's formula."""

# Julian date of J2000.0 (2000-01-01 12:00 TT), the time input as julian date 
# the GMST polynomial is expanded about.
J2000_JD = 2451545.0

SECONDS_PER_DAY = 86400.0

# Julian centuries per day, for the GMST polynomial.
DAYS_PER_JULIAN_CENTURY = 36525.0


def gmst_radians(jd: float) -> float:
    """ Calculates Greenwich Mean Sidereal Time (i.e. how far the Earth has rotated).

    GMST is the angle between the Greenwich meridian and the mean vernal
    equinox (german: Frühlingspunkt) — precisely the angle the TEME frame has to be rotated
    through to line up with the Earth.
    
    First the julian date input jd is represented as 

    t = (jd - J2000_JD) / DAYS_PER_JULIAN_CENTURY

    hence as the time since 01/01/2000 12:00 (0h) in julian centuries, which is 
    the standard time unit in astronomical equations.

    The IAU (International Astronomical Union) 1982 expression 

    gmst_sec =   67310.54841
                 + (86400 * 36525 + 8640184.812866) * t
                 + 0.093104 * t**2
                 - 6.2e-6 * t**3

    approximates the GMST in arcseconds as a cubic polynome:
        - 67310.54841 (constant):
          GMST at J2000.0

        - 8640184.812866 * t (linear term):
          cumulative time diff the sidereal day and the 
          solar day over the course of a century. Since a sidereal day is 
          approximately 3 minutes and 56 seconds shorter than a 24-hour solar day, 
          the sidereal clock gains exactly this amount each day 
          (36,525 × 236.555 s ≈ 86,401,848.1 s)
          plus the real rotations of earth in a century
          (86,400 s x 36,525)

        - 0.093104 * t**2 (quadratic term): 
          models the slow decelaration of earth rotation by tidal friction and the
          precession of equinoxes

        - 6.2e-6 * t**3 (cubic term):
          third-order correction term for long-term orbital and rotational changes
          over centuries  

    to get the GMST only for the day in question we reduce all full 360° turns since J2000.0
    that by modulo 86400 and to arrive at a result theta in radians:

    theta_rad = (gmst_sec % 86400.0) * (2 * np.pi /86400.0)


    Args:
        jd: Julian date. should be UT1; but this pipeline passes UTC and
            accepts up to 0.9 s (~0.4 km of rotation) of error, as the
            module docstring explains.

    Returns:
        GMST in radians, in [0, 2*pi).

    """
    # polynomial coefficients for IAU 1982 formula:
    p0 = 67310.54841
    p1 = 86400 * 36525 + 8640184.812866
    p2 = 0.093104
    p3 = 6.2e-6

    # julian centuries since J2000
    t = (jd - J2000_JD) / DAYS_PER_JULIAN_CENTURY
    # polynome:
    gmst_sec = p0 + p1 * t + p2 * t**2 - p3 * t**3
    # conversion into radians (2 * pi = 86400 sec)
    return (gmst_sec % SECONDS_PER_DAY) * (2 * np.pi / SECONDS_PER_DAY)

def teme_to_ecef(
    teme_position_km: tuple[float, float, float], gmst_rad: float
) -> tuple[float, float, float]:
    """Rotates a fixed-in-space TEME position into an Earth-fixed frame.

    TEME and ECEF share an origin and a Z axis; they differ only by
    rotation about Z, by the angle the Earth has turned. So this is a
    single 2-D rotation with Z carried through untouched::

        x' =  x * cos(theta) + y * sin(theta)
        y' = -x * sin(theta) + y * cos(theta)
        z' =  z

    Args:
        teme_position_km: TEME position as ``(x, y, z)`` in km, exactly as
            SGP4 returns it.
        gmst_rad: Greenwich Mean Sidereal Time in radians, from
            `gmst_radians`.

    Returns:
        The same position as ``(x, y, z)`` in km, in the Earth-fixed
        frame. Strictly PEF rather than ITRF, since polar motion is not
        applied.

    """

    x, y, z = teme_position_km 
    
    cos_theta = np.cos(gmst_rad)
    sin_theta = np.sin(gmst_rad)

    # rotation around z
    x_ecef =  x * cos_theta + y * sin_theta
    y_ecef = -x * sin_theta + y * cos_theta
    z_ecef =  z

    return (x_ecef, y_ecef, z_ecef)



def geodetic_to_ecef(
    latitude_deg: float, longitude_deg: float, altitude_km: float
) -> tuple[float, float, float]:
    """Convert geodetic coordinates to an Earth-fixed position.

    The easy direction. Unlike its inverse this is closed-form and exact,
    with no approximation to get wrong — which is why it served as the
    oracle for `ecef_to_geodetic` before it was needed in its own right.

    It is needed now to place an *observer*: look angles are computed
    from the vector between a point on the ground and a satellite, and
    that subtraction only means anything with both in the same frame.

    Args:
        latitude_deg: Geodetic latitude in degrees.
        longitude_deg: Longitude in degrees.
        altitude_km: Height above the WGS84 ellipsoid in km.

    Returns:
        ``(x, y, z)`` in km.
    """
    latitude = np.radians(latitude_deg)
    longitude = np.radians(longitude_deg)

    # Radius of curvature in the prime vertical: how far it is from the
    # ellipsoid's surface to the polar axis, measured along the normal.
    # Not the distance to the centre — that is the whole reason geodetic
    # and geocentric latitude differ.
    prime_vertical = WGS84_A_KM / np.sqrt(1 - WGS84_E2 * np.sin(latitude) ** 2)

    return (
        float((prime_vertical + altitude_km) * np.cos(latitude) * np.cos(longitude)),
        float((prime_vertical + altitude_km) * np.cos(latitude) * np.sin(longitude)),
        # The (1 - e^2) factor is the ellipsoid's signature: the z axis is
        # foreshortened relative to the equatorial plane by exactly that.
        float((prime_vertical * (1 - WGS84_E2) + altitude_km) * np.sin(latitude)),
    )


def ecef_to_geodetic(
    ecef_position_km: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Converts an Earth-fixed position to WGS84 latitude, longitude and altitude.

    Longitude is easy: it is just the angle in the equatorial plane.
    lon = atan2(y, x)

    Latitude is not, because geodetic latitude is measured from the
    ellipsoid NORMAL, which does not pass through the centre of the
    Earth. That makes latitude implicit in the ellipsoid equation, so it
    is normally found by iteration:

        If the Earth were a perfect sphere, we wouldn't need this loop. 
        The latitude would simply be the angle from the center of the Earth upward.

        However, because the Earth is thicker at the equator due to its rapid 
        rotation (it is, so to speak, “flattened” at the poles), one stands slightly 
        askew in space when on the Earths surface. A plumb line (the line of gravity) 
        does not point exactly to the center of the Earth on an ellipsoid 
        (except at the equator and the poles).

        To calculate exactly where this plumb line vector intersects the equatorial 
        plane, we need these formulas. Here is the exact mathematical breakdown:

        
        1. The distance from the axis of rotation (p)

        p = sqrt(x_ecef**2 + y_ecef**2)
         
        Imagine youre looking down at the Earth from a point directly above 
        the North Pole. The Z-axis points straight into your eye.
        p is simply the Pythagorean theorem applied to the X and Y axes. 
        Its the horizontal distance from the satellite to the Earths axis 
        (essentially the radius of the circle of latitude on which it lies).

        2. The Initial Value (The First Estimate)
        
        lat_rad = atan2(z_ecef, p * (1 - WGS84_E2))
        
        this is our first guess at the latitude.
        We take the altitude Z and divide it by the horizontal distance p. 
        Since we know that the Earth is flattened, we correct the distance p 
        directly using the Earths flattening (1 - e**2).
        This initial value is usually already 99.9% accurate for satellites.

        3. The Chicken-and-Egg Problem (Why We Have to Iterate)
        To calculate the exact latitude on an ellipsoid, we need a specific 
        geodetic parameter called N (the semi-major axis of curvature). 
        This is the radius of the Earths curvature at the exact point where 
        the satellite is hovering above the Earth.
        Heres the mathematical problem:
        To calculate N, we need the exact latitude.
        To calculate the exact latitude, we need N.
        Since the formula cannot simply be solved for latitude, mathematicians 
        use a trick: iteration (approximation).       

        4. The iteration loop:

        for _ in range(2):
            N = WGS84_A / sqrt(1 - WGS84_E2 * sin(lat_rad)**2)
            lat_rad = atan2(z_ecef + N * WGS84_E2 * sin(lat_rad), p)

        We use our good initial value to tackle the problem step by step:
        Loop 1: We take our estimated lat_rad (initial value) and plug it into the 
        classic WGS84 formula for the radius of curvature:

        N = A / sqrt(1 - e**2 * sin(theta)**2)
        (A = Earths radius, e² = eccentricity, ϕ = latitude)

        We now plug this newly calculated N into the formula for latitude 
        (arctan2...) to obtain a much better, more precise latitude.
        Loop 2: We take this new, more precise latitude and calculate an even 
        more accurate N. We then use this to calculate the final latitude.
        
        Why are exactly 2 iterations sufficient?
        Because the Earth is nearly a sphere (the oblateness e**2 is extremely 
        small at 0.0066), this formula converges extremely quickly. 
        After the second round, the result doesnt change again until around the 
        9th decimal place. For a satellite, this means sub-millimeter precision—which 
        is more than sufficient.

    Bowring's formula gets it in one shot, to well under a millimetre
    for any altitude this project will see::

        p     = sqrt(x**2 + y**2)
        theta = atan2(z * a, p * b)

        lat = atan2(z + ep2 * b * sin(theta)**3,
                    p - e2  * a * cos(theta)**3)
        lon = atan2(y, x)

        N   = a / sqrt(1 - e2 * sin(lat)**2)
        alt = p / cos(lat) - N

    `theta` is a parametric angle used only to seed the real one; it
    has no physical meaning on its own.

    Two things to be careful about:

    - `atan2(y, x)` takes Y FIRST. Reversing the arguments yields a
      longitude reflected about 45 degrees, which still looks like a
      real place.
    - `alt = p / cos(lat) - N` degenerates at the poles, where `p` is 0
      and `cos(lat)` is 0. The alternative `z / sin(lat) - N * (1 - e2)`
      is well-behaved there. A satellite exactly over a pole is
      vanishingly unlikely, but `test_ecef_to_geodetic_north_pole`
      exercises it.

    Args:
        ecef_position_km: Earth-fixed position as ``(x, y, z)`` in km.

    Returns:
        ``(latitude_deg, longitude_deg, altitude_km)``. Latitude in
        [-90, 90], longitude in (-180, 180], altitude measured from the
        ELLIPSOID — not from sea level and not from terrain.

    """
    x, y, z = ecef_position_km

    # Longitude
    lon   = np.arctan2(y, x) 

    # initial values for latitude
    p     = np.sqrt(x**2 + y**2)
    theta = np.arctan2(z * WGS84_A_KM, p * WGS84_B_KM)

    # Latitude using Bowring's formula
    lat   = np.arctan2(z + WGS84_EP2 * WGS84_B_KM * np.sin(theta)**3,
                     p - WGS84_E2  * WGS84_A_KM * np.cos(theta)**3)
    
    # initial value for altitude
    n     = WGS84_A_KM / np.sqrt(1 - WGS84_E2 * np.sin(lat)**2)

    # Altitude
    # to avoid singularities at the pole we pick the right formula, flip point is at LAT = 45°
    if np.abs(np.cos(lat)) > np.abs(np.sin(lat)):
        alt  = p / np.cos(lat) - n
    else:
        alt  = z / np.sin(lat) - n * (1 - WGS84_E2)

    return (np.degrees(lat), np.degrees(lon), alt)



def ecef_to_look_angles(
    observer_ecef: tuple[float, float, float],
    satellite_ecef: tuple[float, float, float],
    observer_latitude_deg: float,
    observer_longitude_deg: float,
) -> tuple[float, float, float]:
    """Where a satellite appears from a point on the ground.

    Everything so far has answered "where is this object". This answers
    "where do I look" — which is a different question, and needs the
    observer in the picture.

    ── The three steps ──────────────────────────────────────────────
    1. The **range vector**: the satellite's position minus the
       observer's, still in ECEF axes::

           d = satellite_ecef - observer_ecef

    2. Rotate that vector into the observer's local **East-North-Up**
       frame. With observer latitude phi and longitude lambda::

           E = -sin(lambda)*dx           + cos(lambda)*dy
           N = -sin(phi)*cos(lambda)*dx  - sin(phi)*sin(lambda)*dy  + cos(phi)*dz
           U =  cos(phi)*cos(lambda)*dx  + cos(phi)*sin(lambda)*dy  + sin(phi)*dz

    3. Read the angles off::

           range     = sqrt(dx**2 + dy**2 + dz**2)
           elevation = asin(U / range)
           azimuth   = atan2(E, N)

    ── The trap ────────────────────────────────────────────────────
    Rotate the RANGE VECTOR, not the satellite's own position. Passing
    `satellite_ecef` straight into the rotation gives the direction from
    the centre of the Earth, which can be wrong by up to 90 degrees and
    still looks like a plausible bearing. The subtraction in step 1 is
    the entire point of the function.

    ── Two smaller ones ────────────────────────────────────────────
    `atan2(E, N)` takes East first, not North. Azimuth is measured
    clockwise FROM north, so north is the second argument — the opposite
    order to the usual `atan2(y, x)`.

    That also means the result runs (-180, 180] and has to be normalised
    to [0, 360): due west comes out of `atan2` as -90 and must be
    reported as 270.

    Args:
        observer_ecef: The observer's Earth-fixed position in km, from
            `geodetic_to_ecef`.
        satellite_ecef: The satellite's Earth-fixed position in km, from
            `teme_to_ecef`. Both must be in the SAME frame at the SAME
            instant, or the difference between them is meaningless.
        observer_latitude_deg: Observer geodetic latitude, needed for the
            rotation. Taken as an argument rather than recovered from
            `observer_ecef`, since the caller already has it and
            inverting would reintroduce Bowring's approximation.
        observer_longitude_deg: Observer longitude, likewise.

    Returns:
        ``(azimuth_deg, elevation_deg, range_km)``. Azimuth in [0, 360)
        clockwise from north; elevation in [-90, 90], positive above the
        horizon; range is the straight-line distance, not ground
        distance.
    """
    # subtract observers position in ECEF from satellites position in ECEF:
    # resulting in the vector pointing from the position of the observer on earths surface
    # to the satellite. This is a free vector (no longer pointing from the origin) that
    # already has the right direction and origin in the observers location, 

    d = np.array(satellite_ecef) - np.array(observer_ecef)

    # We only need to rotate the coordinate system (passive rotation) to express the direction
    # in the desired coordinates E, N, U which are not angles but linear cartesian coordinates
    # in East, North, Up direction 

    lam = np.radians(observer_longitude_deg)
    phi = np.radians(observer_latitude_deg)
    E = -np.sin(lam)*d[0]              + np.cos(lam)*d[1]
    N = -np.sin(phi)*np.cos(lam)*d[0]  - np.sin(phi)*np.sin(lam)*d[1]  + np.cos(phi)*d[2]
    U =  np.cos(phi)*np.cos(lam)*d[0]  + np.cos(phi)*np.sin(lam)*d[1]  + np.sin(phi)*d[2]  

    # We can get elevation and azimuth from that using
    range_km     = np.linalg.norm(d)
    elevation = np.arcsin(U / range_km)
    azimuth   = np.arctan2(E, N)

    return (float(np.degrees(azimuth)%360), float(np.degrees(elevation)), float(range_km))



def teme_to_geodetic(
    teme_position_km: tuple[float, float, float], jd: float
) -> tuple[float, float, float]:
    """Convert an SGP4 TEME position straight to WGS84 geodetic coordinates.

    Composition of the three steps above, and the only function the
    propagation step needs to call.

    Args:
        teme_position_km: TEME position as ``(x, y, z)`` in km, as SGP4
            returns it.
        jd: Julian date of the instant the position describes.

    Returns:
        ``(latitude_deg, longitude_deg, altitude_km)``.

    """
    return ecef_to_geodetic(teme_to_ecef(teme_position_km, gmst_radians(jd)))

