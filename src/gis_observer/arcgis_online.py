"""Lookups against ArcGIS Online itself: which organisation owns an item or a Hub site.

Every Hub site and every arcgis.com item belongs to an ArcGIS Online organisation,
and the organisation's portal (<urlKey>.maps.arcgis.com) can be harvested even when
the thing that pointed at it can't.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .classify import canonical
from .http import HttpGetter

ARCGIS_SHARING = "https://www.arcgis.com/sharing/rest"
# Hub's own record of each site's hostname: {"orgId": ..., "orgKey": ..., "siteId": ...}.
# Answers for custom domains (gis.data.mass.gov) as well as *.opendata.arcgis.com.
HUB_DOMAINS = "https://hub.arcgis.com/api/v3/domains"


async def org_portal(http: HttpGetter, org_id: str) -> str | None:
    """The organisation's portal URL, e.g. https://eastlongmeadow.maps.arcgis.com."""
    org = (await http.get(f"{ARCGIS_SHARING}/portals/{org_id}", params={"f": "json"})).json()
    if not isinstance(org, dict) or not org.get("urlKey"):
        return None
    domain = org.get("customBaseUrl") or "maps.arcgis.com"
    return canonical(f"https://{org['urlKey']}.{domain}")  # urlKey can be mixed case


async def hub_site_org(http: HttpGetter, url: str) -> str | None:
    """The id of the organisation that owns the Hub site serving `url`, if it is one."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return None
    site = (await http.get(f"{HUB_DOMAINS}/{host}")).json()
    if isinstance(site, dict) and isinstance(site.get("orgId"), str) and site["orgId"]:
        return site["orgId"]
    return None


async def hub_site_portal(http: HttpGetter, url: str) -> str | None:
    """The portal of the organisation that owns the Hub site serving `url`."""
    org_id = await hub_site_org(http, url)
    return await org_portal(http, org_id) if org_id else None
