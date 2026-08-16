"""SGP4 propagation: element sets in, positions on the Earth out.

`frames` converts the TEME coordinates SGP4 returns into WGS84 geodetic
latitude, longitude and altitude. `elements` turns warehouse rows into
the `Satrec` objects SGP4 propagates.
"""
