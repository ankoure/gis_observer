"""OGC web services: WMS and WFS GetCapabilities.

Covers GeoServer, MapServer, QGIS Server, deegree, and the WMS/WFS that state GIS
offices (e.g. MassGIS) run alongside or instead of ArcGIS.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from ..models import Asset, Platform
from ..util import compact, finite_bbox, srid_from, trim
from .base import Harvester


def parse_xml(content: bytes) -> Element | None:
    try:
        return fromstring(content)
    except (ParseError, DefusedXmlException, ValueError):
        return None


def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child(el: Element | None, name: str) -> Element | None:
    if el is None:
        return None
    return next((c for c in el if local(c.tag) == name), None)


def children(el: Element | None, name: str) -> list[Element]:
    return [] if el is None else [c for c in el if local(c.tag) == name]


def text(el: Element | None, name: str) -> str | None:
    node = child(el, name)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


class OGCHarvester(Harvester):
    platform = Platform.OGC

    async def harvest(self, endpoint):
        base = endpoint.url
        answered = False
        for service, parse in (("WMS", wms_assets), ("WFS", wfs_assets)):
            resp = await self.http.get(
                base,
                params={"service": service, "request": "GetCapabilities"},
                max_bytes=100_000_000,
            )
            if resp.error or resp.status is None or resp.status >= 400:
                continue
            root = parse_xml(resp.content)
            if root is None or not local(root.tag).endswith("Capabilities"):
                continue  # usually an ExceptionReport: this server doesn't offer that service
            answered = True
            for asset in parse(root, base):
                yield asset
        if not answered:
            self.errors.append(f"{base}: no WMS or WFS capabilities")


def _service_extra(root: Element) -> dict:
    # updateSequence identifies the catalog version that answered. Load-balanced servers
    # can have nodes out of sync (MassGIS returned 27440 and 27784 seconds apart), which
    # shows up as layers flapping between harvests; this makes that traceable.
    return compact({"version": root.get("version"), "updateSequence": root.get("updateSequence")})


def wms_assets(root: Element, base: str) -> list[Asset]:
    service_key = f"{base}#WMS"
    service = child(root, "Service")
    out = [
        Asset(
            key=service_key,
            kind="service",
            name="WMS",
            title=text(service, "Title"),
            url=base,
            service_type="WMS",
            description=trim(text(service, "Abstract")),
            extra=_service_extra(root),
        )
    ]
    for layer in root.iter():
        if local(layer.tag) != "Layer" or not (name := text(layer, "Name")):
            continue  # unnamed layers are only containers for their children
        crs = [c.text.strip() for c in layer if local(c.tag) in ("CRS", "SRS") and c.text]
        bbox = _wms_bbox(layer)
        out.append(
            Asset(
                key=f"{base}#WMS:{name}",
                kind="layer",
                name=name,
                title=text(layer, "Title"),
                url=base,
                parent_key=service_key,
                service_type="WMS",
                format="WMS",
                srid=srid_from(crs[0]) if crs else None,
                bbox=bbox,
                bbox_srid=4326 if bbox else None,
                description=trim(text(layer, "Abstract")),
                extra=compact({"queryable": layer.get("queryable") == "1"}),
            )
        )
    return out


def _wms_bbox(layer: Element):
    geo = child(layer, "EX_GeographicBoundingBox")  # WMS 1.3.0
    if geo is not None:
        return finite_bbox(
            text(geo, "westBoundLongitude"),
            text(geo, "southBoundLatitude"),
            text(geo, "eastBoundLongitude"),
            text(geo, "northBoundLatitude"),
        )
    old = child(layer, "LatLonBoundingBox")  # WMS 1.1.x
    if old is not None:
        return finite_bbox(old.get("minx"), old.get("miny"), old.get("maxx"), old.get("maxy"))
    return None


def wfs_assets(root: Element, base: str) -> list[Asset]:
    service_key = f"{base}#WFS"
    ident = child(root, "ServiceIdentification")
    if ident is None:
        ident = child(root, "Service")  # WFS 1.0
    out = [
        Asset(
            key=service_key,
            kind="service",
            name="WFS",
            title=text(ident, "Title"),
            url=base,
            service_type="WFS",
            description=trim(text(ident, "Abstract")),
            extra=_service_extra(root),
        )
    ]
    for ft in children(child(root, "FeatureTypeList"), "FeatureType"):
        name = text(ft, "Name")
        if not name:
            continue
        bbox = _wfs_bbox(ft)
        crs = text(ft, "DefaultCRS") or text(ft, "DefaultSRS") or text(ft, "SRS")
        out.append(
            Asset(
                key=f"{base}#WFS:{name}",
                kind="layer",
                name=name,
                title=text(ft, "Title"),
                url=base,
                parent_key=service_key,
                service_type="WFS",
                format="WFS",
                srid=srid_from(crs),
                bbox=bbox,
                bbox_srid=4326 if bbox else None,
                description=trim(text(ft, "Abstract")),
            )
        )
    return out


def _wfs_bbox(ft: Element):
    wgs = child(ft, "WGS84BoundingBox")  # WFS 1.1 / 2.0: "lon lat" corners
    if wgs is not None:
        lower = (text(wgs, "LowerCorner") or "").split()
        upper = (text(wgs, "UpperCorner") or "").split()
        if len(lower) == 2 and len(upper) == 2:
            return finite_bbox(lower[0], lower[1], upper[0], upper[1])
        return None
    old = child(ft, "LatLongBoundingBox")  # WFS 1.0
    if old is not None:
        return finite_bbox(old.get("minx"), old.get("miny"), old.get("maxx"), old.get("maxy"))
    return None
