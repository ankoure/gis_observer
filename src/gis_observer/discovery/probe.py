"""Work out what's behind a candidate URL the offline classifier couldn't place.

In order: resolve ArcGIS Online item links to their organisation; fetch the URL and
classify where it redirected to; recognise catalog APIs by their JSON shape; scan an
HTML page (usually a town's "Maps & GIS" page) for links and embedded maps; and as a
last resort try the conventional paths on gis.* / data.* subdomains.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..classify import ITEM_ID, canonical, classify, host_matches
from ..http import HttpGetter, Response
from ..models import Endpoint, Platform
from .links import extract_links

ARCGIS_SHARING = "https://www.arcgis.com/sharing/rest"
GIS_SUBDOMAINS = {"gis", "maps", "map", "gisweb", "webgis", "geo", "arcgis", "agsweb"}
DATA_SUBDOMAINS = {"data", "opendata", "gisdata", "hub"}
CKAN_GENERATOR = re.compile(rb"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']ckan", re.I)


@dataclass
class ProbeResult:
    status: str  # found | none | dead
    endpoints: list[Endpoint] = field(default_factory=list)


async def probe(http: HttpGetter, url: str, text: str | None = None) -> ProbeResult:
    found = await resolve_arcgis_item(http, url)
    if found:
        return ProbeResult("found", _dedupe(found))

    resp = await http.get(url, max_bytes=10_000_000)
    if resp.status is None or resp.status >= 400:
        found = await _well_known(http, url)
        return ProbeResult("found" if found else "dead", found)

    if (ep := classify(resp.url, text)) is not None:
        found.append(ep)
    found += _sniff_json(resp)
    if not found and "html" in resp.headers.get("content-type", "").lower():
        found += await _scan_page(http, resp)
    if not found:
        found += await _well_known(http, resp.url)
    return ProbeResult("found" if found else "none", _dedupe(found))


async def resolve_arcgis_item(http: HttpGetter, url: str) -> list[Endpoint]:
    """www.arcgis.com/apps/...?id=, experience.arcgis.com/experience/<id>, dashboards...

    The item says which organisation owns it (-> its portal) and, for service items,
    which server hosts the data.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host_matches(host, "arcgis.com") or classify(url) is not None:
        return []
    match = ITEM_ID.search(f"{parts.path}?{parts.query}")
    if not match:
        return []
    item = (
        await http.get(f"{ARCGIS_SHARING}/content/items/{match.group(0)}", params={"f": "json"})
    ).json()
    if not isinstance(item, dict) or not item.get("orgId"):
        return []
    found = []
    org = (await http.get(f"{ARCGIS_SHARING}/portals/{item['orgId']}", params={"f": "json"})).json()
    if isinstance(org, dict) and org.get("urlKey"):
        domain = org.get("customBaseUrl") or "maps.arcgis.com"
        portal = canonical(f"https://{org['urlKey']}.{domain}")  # urlKey can be mixed case
        found.append(Endpoint(Platform.ARCGIS_PORTAL, portal))
    if item.get("url") and (ep := classify(item["url"])) is not None:
        found.append(ep)
    return found


def _sniff_json(resp: Response) -> list[Endpoint]:
    data = resp.json()
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("dataset"), list):
        return [Endpoint(Platform.DCAT, canonical(resp.url))]
    if "help" in data and "success" in data and "/api/3/" in resp.url:
        return [Endpoint(Platform.CKAN, canonical(resp.url.split("/api/3/")[0]))]
    return []


async def _scan_page(http: HttpGetter, resp: Response) -> list[Endpoint]:
    found, items = [], []
    for link, text in extract_links(resp.text, resp.url):
        if (ep := classify(link, text)) is not None:
            found.append(ep)
        elif host_matches((urlsplit(link).hostname or "").lower(), "arcgis.com") and ITEM_ID.search(
            link
        ):
            items.append(link)
    for link in items[:5]:
        found += await resolve_arcgis_item(http, link)
    if CKAN_GENERATOR.search(resp.content[:50_000]):
        parts = urlsplit(resp.url)
        found.append(Endpoint(Platform.CKAN, canonical(f"{parts.scheme}://{parts.netloc}")))
    return found


async def _well_known(http: HttpGetter, url: str) -> list[Endpoint]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    label = host.split(".")[0]
    origin = f"{parts.scheme}://{parts.netloc}"
    found: list[Endpoint] = []
    if label in GIS_SUBDOMAINS:
        for path in ("/arcgis/rest/services", "/server/rest/services", "/gis/rest/services"):
            data = (await http.get(origin + path, params={"f": "json"}, max_bytes=5_000_000)).json()
            if isinstance(data, dict) and "currentVersion" in data:
                found.append(Endpoint(Platform.ARCGIS_REST, canonical(origin + path)))
                break
        caps = await http.get(
            origin + "/geoserver/ows",
            params={"service": "WMS", "request": "GetCapabilities"},
            max_bytes=0,
        )
        if caps.ok and "xml" in caps.headers.get("content-type", ""):
            found.append(
                Endpoint(Platform.OGC, canonical(origin + "/geoserver/ows"), vendor="GeoServer")
            )
    if label in DATA_SUBDOMAINS:
        feed = await http.get(origin + "/data.json", max_bytes=0)
        if feed.ok and "json" in feed.headers.get("content-type", ""):
            found.append(Endpoint(Platform.DCAT, canonical(origin + "/data.json")))
        status = (await http.get(origin + "/api/3/action/status_show", max_bytes=1_000_000)).json()
        if isinstance(status, dict) and status.get("success"):
            found.append(Endpoint(Platform.CKAN, canonical(origin)))
    return found


def _dedupe(endpoints: list[Endpoint]) -> list[Endpoint]:
    seen: dict[str, Endpoint] = {}
    for ep in endpoints:
        seen.setdefault(ep.id, ep)
    return list(seen.values())
