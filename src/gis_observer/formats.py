"""Recognise geospatial formats from the labels catalogs give them, media types, or URLs."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

GEO_FORMATS: dict[str, set[str]] = {
    "Shapefile": {"shp", "shapefile", "esri shapefile", "zip (shapefile)", "shape", "shape file"},
    "GeoJSON": {"geojson", "geo json", "application/geo+json", "application/vnd.geo+json"},
    "KML": {"kml", "application/vnd.google-earth.kml+xml"},
    "KMZ": {"kmz", "application/vnd.google-earth.kmz"},
    "GeoPackage": {"gpkg", "geopackage", "application/geopackage+sqlite3", "application/x-gpkg"},
    "File Geodatabase": {"gdb", "fgdb", "filegdb", "file geodatabase", "esri file geodatabase"},
    "Esri REST": {
        "esri rest",
        "esri rest api",
        "arcgis geoservices rest api",
        "arcgis rest api",
        "arcgis rest",
        "arcgis server",
        "feature service",
        "map service",
    },
    "WMS": {"wms", "ogc wms", "ogc:wms"},
    "WFS": {"wfs", "ogc wfs", "ogc:wfs"},
    "WMTS": {"wmts", "ogc wmts", "ogc:wmts"},
    "GML": {"gml", "application/gml+xml"},
    "TopoJSON": {"topojson"},
    "GeoTIFF": {"geotiff", "geotif", "image/tiff; application=geotiff"},
    "LAS/LAZ": {"las", "laz", "lidar"},
}
SERVICE_FORMATS = {"Esri REST", "WMS", "WFS", "WMTS"}

_ALIASES = {alias: name for name, aliases in GEO_FORMATS.items() for alias in aliases}
_ALIASES.update({name.lower(): name for name in GEO_FORMATS})

# Longest first, so ".shp.zip" wins over ".zip"-style partial matches.
_EXTENSIONS = sorted(
    {
        ".geojson": "GeoJSON",
        ".kml": "KML",
        ".kmz": "KMZ",
        ".gpkg": "GeoPackage",
        ".shp": "Shapefile",
        ".shp.zip": "Shapefile",
        ".gdb.zip": "File Geodatabase",
        ".topojson": "TopoJSON",
        ".gml": "GML",
        ".las": "LAS/LAZ",
        ".laz": "LAS/LAZ",
    }.items(),
    key=lambda kv: -len(kv[0]),
)

# Download APIs that put the format in the last path segment, e.g. ArcGIS Hub's
# /api/download/v1/items/<id>/shapefile and Google My Maps' /maps/d/kml.
_PATH_FORMATS = {
    "shapefile": "Shapefile",
    "geojson": "GeoJSON",
    "kml": "KML",
    "filegdb": "File Geodatabase",
    "geopackage": "GeoPackage",
}


def geo_format(fmt: str | None = None, url: str | None = None, media_type: str | None = None):
    """Normalised geospatial format name, or None if nothing marks this as geospatial."""
    for label in (fmt, media_type):
        if label and (name := _ALIASES.get(str(label).strip().lower())):
            return name
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = parts.path.lower()
    if "/rest/services/" in path + "/":
        return "Esri REST"
    for ext, name in _EXTENSIONS:
        if path.endswith(ext):
            return name
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if last in _PATH_FORMATS:
        return _PATH_FORMATS[last]
    query = {k.lower(): v.lower() for k, v in parse_qsl(parts.query)}
    if query.get("service") in ("wms", "wfs", "wmts"):
        return query["service"].upper()
    asked = query.get("format") or query.get("outputformat") or query.get("f")
    return _ALIASES.get(asked) if asked else None
