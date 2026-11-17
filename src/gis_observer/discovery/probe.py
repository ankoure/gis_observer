"""Work out what's behind a candidate URL the offline classifier couldn't place.

In order: resolve ArcGIS Online item links to their organisation; fetch the URL and
classify where it redirected to; recognise catalog APIs by their JSON shape; scan an
HTML page (usually a town's "Maps & GIS" page) for links and embedded maps; recognise
Hub sites on custom domains; and as a last resort try the conventional paths on
gis.* / data.* subdomains.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..arcgis_online import ARCGIS_SHARING, hub_site_org, org_portal
from ..classify import ITEM_ID, canonical, classify, host_matches
from ..http import HttpGetter, Response
from ..models import Endpoint, Platform
from .links import extract_links

GIS_SUBDOMAINS = {"gis", "maps", "map", "gisweb", "webgis", "geo", "arcgis", "agsweb"}
DATA_SUBDOMAINS = {"data", "opendata", "gisdata", "hub"}
CKAN_GENERATOR = re.compile(rb"<meta[^>]+name=[\"']generator[\"'][^>]+content=[\"']ckan", re.I)
# Hub site pages are built by Hub's "opendata-ui" app and load from hub.arcgis.com.
HUB_PAGE = re.compile(rb"opendata-ui|hub\.arcgis\.com", re.I)


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
        if not found and HUB_PAGE.search(resp.content[:200_000]):
            found += await resolve_hub_site(http, resp.url)
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
    if portal := await org_portal(http, item["orgId"]):
        found.append(Endpoint(Platform.ARCGIS_PORTAL, portal))
    if item.get("url") and (ep := classify(item["url"])) is not None:
        found.append(ep)
    return found


async def resolve_hub_site(http: HttpGetter, url: str) -> list[Endpoint]:
    """A Hub site on a custom domain (gis.data.mass.gov): its feed and its owner's portal.

    The portal is the fallback for when the site's feed is broken.
    """
    org_id = await hub_site_org(http, url)
    if not org_id:
        return []
    parts = urlsplit(url)
    feed = canonical(f"{parts.scheme}://{parts.netloc}/api/feed/dcat-us/1.1.json")
    found = [Endpoint(Platform.DCAT, feed, vendor="ArcGIS Hub")]
    if portal := await org_portal(http, org_id):
        found.append(Endpoint(Platform.ARCGIS_PORTAL, portal))
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
