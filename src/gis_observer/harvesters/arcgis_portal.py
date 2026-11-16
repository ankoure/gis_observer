"""ArcGIS Online organisations and ArcGIS Enterprise portals.

Towns often link to an ArcGIS Online web app rather than a server. The org's public
items list the services behind those apps: hosted feature services on
services*.arcgis.com, and frequently the town's or county's own ArcGIS Server. Each
service URL is yielded as a candidate, so the REST harvester picks up its layers.
"""

from __future__ import annotations

from ..models import Asset, Platform
from ..util import compact, finite_bbox, parse_when, trim
from .base import Harvester

ITEM_KINDS = {
    "Feature Service": "service",
    "Map Service": "service",
    "Image Service": "service",
    "Vector Tile Service": "service",
    "Scene Service": "service",
    "WMS": "service",
    "WFS": "service",
    "WMTS": "service",
    "OGCFeatureServer": "service",
    "Shapefile": "file",
    "File Geodatabase": "file",
    "GeoJson": "file",
    "KML": "file",
    "KML Collection": "file",
    "GeoPackage": "file",
    "Web Map": "webmap",
}
SEARCH_LIMIT = 10_000  # ArcGIS search stops paging here


class ArcGISPortalHarvester(Harvester):
    platform = Platform.ARCGIS_PORTAL
    page_size = 100

    async def harvest(self, endpoint):
        base = endpoint.url.rstrip("/")
        api = f"{base}/sharing/rest"
        me = await self.get_json(f"{api}/portals/self", {"f": "json"})
        if not isinstance(me, dict) or not me.get("id"):
            self.errors.append(f"{api}/portals/self: no organisation id")
            return
        org = me["id"]
        yield Asset(
            key=base,
            kind="portal",
            name=me.get("urlKey") or org,
            title=me.get("name"),
            url=base,
            description=trim(me.get("description")),
            extra={"org_id": org},
        )

        # Search's type matching is fuzzy ("Web Map" also matches "Web Mapping
        # Application"), so the query narrows things down and ITEM_KINDS filters exactly.
        types = " OR ".join(f'type:"{t}"' for t in ITEM_KINDS)
        query = f"orgid:{org} AND ({types})"
        start = 1
        while 0 < start <= SEARCH_LIMIT:
            params = {
                "q": query, "num": self.page_size, "start": start, "f": "json",
                "sortField": "created", "sortOrder": "asc",
            }  # fmt: skip
            page = await self.get_json(f"{api}/search", params)
            if not isinstance(page, dict) or "results" not in page:
                if isinstance(page, dict):
                    self.errors.append(f"{api}/search: {page.get('error')}")
                return
            for item in page["results"]:
                kind = ITEM_KINDS.get(item.get("type"))
                if kind is None:
                    continue
                key = f"{base}/home/item.html?id={item.get('id')}"
                yield _item_asset(key, kind, item)
                if item.get("url"):
                    yield self.candidate(
                        endpoint, item["url"], found_on=key, text=item.get("title")
                    )
            start = page.get("nextStart", -1)


def _item_asset(key: str, kind: str, item: dict) -> Asset:
    bbox = None
    extent = item.get("extent")
    if isinstance(extent, list) and len(extent) == 2:
        try:
            bbox = finite_bbox(extent[0][0], extent[0][1], extent[1][0], extent[1][1])
        except (IndexError, TypeError):
            bbox = None
    return Asset(
        key=key,
        kind=kind,
        name=item.get("name") or item.get("title"),
        title=item.get("title"),
        url=item.get("url") or key,
        service_type=item.get("type"),
        format=item.get("type") if kind == "file" else None,
        bbox=bbox,
        bbox_srid=4326 if bbox else None,
        description=trim(item.get("snippet") or item.get("description")),
        source_updated_at=parse_when(item.get("modified")),
        extra=compact(
            {
                "item_id": item.get("id"),
                "owner": item.get("owner"),
                "access": item.get("access"),
                "tags": item.get("tags") or None,
            }
        ),
    )
