# Assets

## `land.json`

Country outlines, used to draw the landmasses in the map's globe view.

A globe cannot use a raster basemap: map tiles are Mercator-projected
images and cannot be draped on a sphere. So the land has to be supplied
as vector polygons instead.

**Source:** [Natural Earth](https://www.naturalearthdata.com/)
`ne_110m_admin_0_countries`, via
[nvkelso/natural-earth-vector](https://github.com/nvkelso/natural-earth-vector).

**Licence:** public domain. Natural Earth places no restrictions on use.

**Processing:** the published file is ~820 KB, almost all of it metadata —
Natural Earth carries roughly 90 properties per feature (names in many
languages, ISO codes, population estimates) and the map needs none of
them. Every property was dropped and coordinates rounded to two decimal
places, which is about 1 km precision and far finer than a globe at any
useful zoom can show. That takes it to ~165 KB.

To regenerate:

```python
import json

def round_coords(obj, nd=2):
    if isinstance(obj, list):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(c), nd) for c in obj]
        return [round_coords(o, nd) for o in obj]
    return obj

source = json.load(open("ne_110m_admin_0_countries.geojson"))
json.dump(
    {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {
            "type": f["geometry"]["type"],
            "coordinates": round_coords(f["geometry"]["coordinates"])}}
        for f in source["features"] if f.get("geometry")]},
    open("land.json", "w"), separators=(",", ":"),
)
```
