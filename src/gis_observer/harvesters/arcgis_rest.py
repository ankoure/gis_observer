"""ArcGIS REST services directories: ArcGIS Server, Enterprise, and hosted feature servers.

Ported from ArcGISRest_Munger (python/main.py, helpers.py) and the Airflow harvester
(metadata_test.py). The walk is the same: root -> folders -> services. What changed:

- It goes down to layers and tables (the step-5 TODO in metadata_test.py), using one
  `/layers` call per MapServer/FeatureServer instead of one request per layer.
- Service-level fields match the old `service_observation` table: currentVersion,
  serviceDescription, copyrightText, fullExtent, spatialReference.
- The `?f=sitemap` shortcut is dropped: it returned URLs without names or types (the
  Munger left that as a TODO), and the folder walk returns both.
- Secured services ({"error": {"code": 499}} with HTTP 200) become assets marked
  restricted, rather than being skipped.
"""

from __future__ import annotations

from ..models import Asset, Platform
from ..util import compact, finite_bbox, parse_when, srid_from, trim
from .base import Harvester

GEOMETRY = {
    "esriGeometryPoint": "point",
    "esriGeometryMultipoint": "multipoint",
    "esriGeometryPolyline": "line",
    "esriGeometryPolygon": "polygon",
    "esriGeometryEnvelope": "envelope",
    "esriGeometryMultiPatch": "multipatch",
}
LAYER_KINDS = {
    "Feature Layer": "layer",
    "Annotation Layer": "layer",
    "Dimension Layer": "layer",
    "Table": "table",
    "Group Layer": "group",
    "Raster Layer": "raster",
    "Mosaic Layer": "raster",
    "Raster Catalog Layer": "raster",
}
LAYERED = {"MapServer", "FeatureServer"}
F_JSON = {"f": "json"}


def _bbox(extent) -> tuple[tuple[float, float, float, float] | None, int | None]:
    if not isinstance(extent, dict):
        return None, None
    box = finite_bbox(
        extent.get("xmin"), extent.get("ymin"), extent.get("xmax"), extent.get("ymax")
    )
    return box, srid_from(extent.get("spatialReference")) if box else None


class ArcGISRestHarvester(Harvester):
    platform = Platform.ARCGIS_REST

    async def harvest(self, endpoint):
        root = endpoint.url.rstrip("/")
        info = await self.get_json(root, F_JSON)
        if not isinstance(info, dict):
            return
        if "error" in info or "currentVersion" not in info:
            self.errors.append(f"{root}: not a readable services directory ({info.get('error')})")
            return
        yield Asset(
            key=root,
            kind="server",
            name=root,
            url=root,
            extra=compact({"currentVersion": info.get("currentVersion")}),
        )

        services = list(info.get("services") or [])
        for folder in info.get("folders") or []:
            sub = await self.get_json(f"{root}/{folder}", F_JSON)
            if isinstance(sub, dict) and "error" not in sub:  # an error here = secured folder
                services.extend(sub.get("services") or [])

        seen: set[str] = set()
        for svc in services:
            name, stype = svc.get("name"), svc.get("type")
            if not name or not stype:
                continue
            # Folder services come back named "Folder/Service", so root/name/type is right.
            url = svc.get("url") or f"{root}/{name}/{stype}"
            if url in seen:
                continue
            seen.add(url)
            async for asset in self._service(url, name, stype, root):
                yield asset

    async def _service(self, url: str, name: str, stype: str, root: str):
        d = await self.get_json(url, F_JSON)
        if not isinstance(d, dict):
            return
        if "error" in d:
            code = (d.get("error") or {}).get("code")
            yield Asset(
                key=url, kind="service", name=name, url=url, parent_key=root, service_type=stype,
                extra=compact({"access": "restricted", "error_code": code}),
            )  # fmt: skip
            return

        extent = d.get("fullExtent") or d.get("extent") or d.get("initialExtent")
        bbox, bbox_srid = _bbox(extent)
        doc = d.get("documentInfo") or {}
        yield Asset(
            key=url,
            kind="service",
            name=name,
            title=trim(doc.get("Title"), 300),
            url=url,
            parent_key=root,
            service_type=stype,
            srid=srid_from(d.get("spatialReference")),
            bbox=bbox,
            bbox_srid=bbox_srid,
            description=trim(d.get("serviceDescription") or d.get("description")),
            extra=compact(
                {
                    "currentVersion": d.get("currentVersion"),
                    "copyrightText": trim(d.get("copyrightText"), 500),
                    "capabilities": d.get("capabilities"),
                }
            ),
        )
        if stype not in LAYERED:
            return

        detail = await self.get_json(f"{url}/layers", F_JSON, quiet=True)
        if (
            isinstance(detail, dict)
            and "error" not in detail
            and ("layers" in detail or "tables" in detail)
        ):
            layers, tables = detail.get("layers") or [], detail.get("tables") or []
        else:  # servers older than 10.0 have no /layers; use the summaries in the service JSON
            layers, tables = d.get("layers") or [], d.get("tables") or []
        for lyr in layers:
            yield _layer(url, stype, lyr, table=False)
        for tbl in tables:
            yield _layer(url, stype, tbl, table=True)


def _layer(service_url: str, stype: str, lyr: dict, *, table: bool) -> Asset:
    url = f"{service_url}/{lyr.get('id')}"
    ltype = lyr.get("type") or ("Table" if table else None)
    extent = lyr.get("extent")
    bbox, bbox_srid = _bbox(extent)
    editing = lyr.get("editingInfo") or {}
    fields = [
        compact({"name": f.get("name"), "type": f.get("type"), "alias": f.get("alias")})
        for f in lyr.get("fields") or []
    ]
    return Asset(
        key=url,
        kind=LAYER_KINDS.get(ltype or "", "table" if table else "layer"),
        name=lyr.get("name"),
        url=url,
        parent_key=service_url,
        service_type=stype,
        geometry_type=GEOMETRY.get(lyr.get("geometryType") or ""),
        srid=bbox_srid,
        bbox=bbox,
        bbox_srid=bbox_srid,
        description=trim(lyr.get("description")),
        source_updated_at=parse_when(
            editing.get("lastEditDate") or editing.get("dataLastEditDate")
        ),
        extra=compact(
            {
                "layer_type": ltype,
                "fields": fields,
                "maxRecordCount": lyr.get("maxRecordCount"),
                "capabilities": lyr.get("capabilities"),
                "parent_layer": (lyr.get("parentLayer") or {}).get("id"),
            }
        ),
    )
