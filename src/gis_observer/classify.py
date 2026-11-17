"""Decide what platform serves a URL, from the URL alone (no network).

Anything that comes back None is either not GIS or needs a network probe to tell
(see discovery/probe.py).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .formats import SERVICE_FORMATS, geo_format
from .models import Endpoint, Platform

REST_ROOT = re.compile(r"^(?P<root>.*?/rest/services)(?=/|$)", re.I)
# ArcGIS Enterprise portal behind a web adaptor: /portal/home/item.html, /arcgis/apps/...
PORTAL_BASE = re.compile(
    r"^(?P<base>/[^/]+)/(?:home/(?:item|index|search|gallery|webmap)|apps/|sharing/rest)", re.I
)
ITEM_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.I)

# Request parameters that pick an operation rather than identify the service.
OGC_PARAMS = {
    "service", "request", "version", "acceptversions", "layers", "typename", "typenames",
    "bbox", "width", "height", "format", "srs", "crs", "styles", "outputformat",
}  # fmt: skip

# Vendor-hosted municipal map viewers. They show layers but expose no public catalog,
# so they're recorded (who uses which vendor) but not harvested.
VIEWER_HOSTS = {
    "axisgis.com": "AxisGIS",
    "mapgeo.io": "MapGeo",
    "mapsonline.net": "PeopleGIS MapsOnline",
    "schneidercorp.com": "Schneider Beacon",
    "qpublic.net": "Schneider qPublic",
    # Each viewer's assets/js/settings.js names a public ArcGIS Server folder on
    # hostingdataN.tighebond.com (e.g. .../arcgis/rest/services/PalmerMA), so these could
    # be followed to real services later.
    "hosting.tighebond.com": "Tighe & Bond",
    "vgsi.com": "Vision Government Solutions",  # assessor database with parcel maps
}
# One viewer per first path segment (the town), e.g. axisgis.com/ChesterMA/.
PATH_SCOPED_VIEWERS = {"axisgis.com", "hosting.tighebond.com", "mapsonline.net", "vgsi.com"}

# Consumer map and social sites a town links to for directions or news; never a data source.
# live.com covers old Bing Maps links (maps.live.com) and the Office document viewer.
NOT_GIS = (
    "google.com", "goo.gl", "bing.com", "live.com", "yahoo.com", "apple.com", "mapquest.com",
    "openstreetmap.org",
    "waze.com", "yelp.com", "facebook.com", "twitter.com", "x.com", "youtube.com",
    "instagram.com", "linkedin.com",
)  # fmt: skip

# Word-level match, so "sitemap" doesn't count as "map". Adapted from govsearch's GIS_KEYWORDS.
GIS_WORDS = {
    "gis", "arcgis", "map", "maps", "mapping", "geoportal", "spatial", "geospatial",
    "webmap", "mapserver", "geodata", "gisdata", "opendata", "shapefile", "shapefiles",
    "kml", "geoserver", "parcel", "parcels",
}  # fmt: skip
# Words that on their own say "GIS", unlike "map" (street maps, flood map PDFs...).
STRONG_GIS_WORDS = GIS_WORDS - {"map", "maps", "mapping", "parcel", "parcels"}


def canonical(url: str) -> str:
    """Lowercase scheme and host, drop default ports, fragments and trailing slashes."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and (scheme, port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


def host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def classify(url: str, text: str | None = None) -> Endpoint | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    path = parts.path
    lpath = path.lower()

    # Google My Maps is how a lot of small towns publish layers; its KML export is real data.
    if host_matches(host, "google.com") and lpath.startswith("/maps/d/"):
        mid = dict(parse_qsl(parts.query)).get("mid")
        if not mid:
            return None
        kml = f"https://www.google.com/maps/d/kml?mid={mid}&forcekml=1"
        return Endpoint(Platform.FILE, kml, vendor="Google My Maps", label="KML")
    if any(host_matches(host, h) for h in NOT_GIS):
        return None

    if m := REST_ROOT.match(path):
        return Endpoint(Platform.ARCGIS_REST, canonical(origin + m["root"]))

    if host.endswith(".hub.arcgis.com") or host.endswith("opendata.arcgis.com"):
        feed = f"https://{host}/api/feed/dcat-us/1.1.json"
        return Endpoint(Platform.DCAT, feed, vendor="ArcGIS Hub")
    if host.endswith(".maps.arcgis.com"):
        return Endpoint(Platform.ARCGIS_PORTAL, f"https://{host}")
    if host_matches(host, "arcgis.com"):
        return None  # www.arcgis.com/apps/..., experience.arcgis.com: needs an item lookup
    if m := PORTAL_BASE.match(path):
        return Endpoint(Platform.ARCGIS_PORTAL, canonical(origin + m["base"]))

    for suffix, vendor in VIEWER_HOSTS.items():
        if host_matches(host, suffix):
            seg = path.strip("/").split("/")[0] if suffix in PATH_SCOPED_VIEWERS else ""
            return Endpoint(Platform.VIEWER, canonical(f"{origin}/{seg}"), vendor=vendor)

    query = {k.lower(): v.lower() for k, v in parse_qsl(parts.query)}
    if "/geoserver" in lpath:
        root = path[: lpath.index("/geoserver") + len("/geoserver")]
        return Endpoint(Platform.OGC, canonical(f"{origin}{root}/ows"), vendor="GeoServer")
    if (
        query.get("service") in ("wms", "wfs", "wmts", "wcs")
        or query.get("request") == "getcapabilities"
        or re.search(r"/(wms|wfs|ows)$", lpath.rstrip("/"))
    ):
        keep = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in OGC_PARAMS]
        base = urlunsplit((parts.scheme, parts.netloc, path, urlencode(keep), ""))
        return Endpoint(Platform.OGC, canonical(base))

    if lpath.endswith("/data.json") or "/api/feed/dcat" in lpath:
        return Endpoint(Platform.DCAT, canonical(origin + path))
    if "/api/3/action" in lpath:
        return Endpoint(Platform.CKAN, canonical(origin + path[: lpath.index("/api/3/action")]))

    fmt = geo_format(None, url)
    if fmt and fmt not in SERVICE_FORMATS:
        return Endpoint(Platform.FILE, canonical(url), label=fmt)
    if lpath.endswith(".zip") and has_gis_keyword(url, text):
        return Endpoint(Platform.FILE, canonical(url), label="ZIP")
    return None


def has_gis_keyword(url: str, text: str | None = None, *, strong: bool = False) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    haystack = f"{parts.hostname or ''} {parts.path} {text or ''}".lower()
    if "open data" in haystack:
        return True
    words = STRONG_GIS_WORDS if strong else GIS_WORDS
    return not words.isdisjoint(re.findall(r"[a-z0-9]+", haystack))


def is_gis_candidate(url: str, text: str | None = None) -> bool:
    """Worth keeping: either a recognised platform, or GIS words on a non-consumer site."""
    if classify(url, text) is not None:
        return True
    host = (urlsplit(url).hostname or "").lower()
    if any(host_matches(host, h) for h in NOT_GIS):
        return False
    return has_gis_keyword(url, text)
