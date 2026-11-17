"""The records that move between discovery, harvesting and the store."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit


class Platform(StrEnum):
    ARCGIS_REST = "arcgis_rest"  # ArcGIS Server or hosted feature server: .../rest/services
    ARCGIS_PORTAL = "arcgis_portal"  # ArcGIS Online org (<org>.maps.arcgis.com) or Enterprise
    OGC = "ogc"  # WMS / WFS GetCapabilities: GeoServer, MapServer, QGIS Server...
    CKAN = "ckan"  # CKAN open-data portal (/api/3/action)
    DCAT = "dcat"  # DCAT-US data.json feed: Socrata, ArcGIS Hub, and most other portals
    FILE = "file"  # a direct download: shapefile zip, KML, GeoJSON, GeoPackage...
    VIEWER = "viewer"  # vendor map viewer with no public layer API (AxisGIS, MapGeo...)


def stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Jurisdiction:
    id: str  # "osm:relation/1181602", "dotgov:example.gov"
    name: str
    website: str | None = None
    kind: str | None = None  # City, County, State...
    state: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A URL that might lead to GIS data, before we know what serves it."""

    url: str
    via: str  # crawl | govsearch | probe | import:<file> | harvest:<endpoint id>
    found_on: str | None = None
    link_text: str | None = None
    jurisdiction_id: str | None = None


@dataclass(frozen=True)
class Endpoint:
    """One catalog root we know how to read (or, for viewers, at least name)."""

    platform: Platform
    url: str
    vendor: str | None = None
    label: str | None = None

    @property
    def id(self) -> str:
        return stable_id(self.platform, self.identity)

    @property
    def identity(self) -> str:
        """The URL with any case that doesn't matter folded away.

        ArcGIS Server paths are case-insensitive, and links to one server mix
        /arcgis/ and /ArcGIS/. Hosted servers on *.arcgis.com are the exception: the
        org id in services.arcgis.com/<orgId>/ is case-sensitive. `url` itself keeps
        the spelling first seen, since that's one known to work.

        A vendor viewer is reached by many spellings of one URL: http or https, host
        aliases (www.axisgis.com, next.axisgis.com) and any case of the town slug. So a
        viewer is the vendor plus the lowercased town slug for path-scoped viewers
        (axisgis.com/HampdenMA), or the vendor plus the host for the others
        (amherstma.mapgeo.io). classify() keeps a path only for path-scoped viewers.
        """
        if self.platform is Platform.VIEWER:
            parts = urlsplit(self.url)
            slug = parts.path.strip("/").split("/")[0].lower()
            where = f"/{slug}" if slug else (parts.hostname or "").lower()
            return f"{self.vendor or ''}|{where}"
        if self.platform is Platform.ARCGIS_REST:
            host = urlsplit(self.url).hostname or ""
            if not (host == "arcgis.com" or host.endswith(".arcgis.com")):
                return self.url.lower()
        return self.url


@dataclass
class Asset:
    """One catalogued thing: a service, layer, table, dataset, file or portal item.

    `key` is unique within its endpoint; it's the asset's URL wherever one exists.
    """

    key: str
    kind: str  # portal | service | layer | table | group | raster | dataset | file | webmap
    name: str | None = None
    title: str | None = None
    url: str | None = None
    parent_key: str | None = None
    service_type: str | None = None  # MapServer, FeatureServer, WMS, WFS, Esri REST...
    geometry_type: str | None = None  # point | multipoint | line | polygon | ...
    format: str | None = None
    srid: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    bbox_srid: int | None = None
    description: str | None = None
    source_updated_at: datetime | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["source_updated_at"] = (
            self.source_updated_at.isoformat() if self.source_updated_at else None
        )
        return d

    def content_hash(self) -> str:
        blob = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()
