"""Small shared helpers: timestamps, spatial references, bounding boxes, trimming."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

EPSG = re.compile(r"EPSG(?:/0/|::?|:)(\d+)", re.I)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_when(value) -> datetime | None:
    """ISO-8601 text, HTTP dates, or epoch milliseconds (ArcGIS) -> aware UTC datetime."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def srid_from(value) -> int | None:
    """EPSG code from an ArcGIS spatialReference, an OGC CRS string, or a bare number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return srid_from(value.get("latestWkid") or value.get("wkid"))
    text = str(value).strip()
    if text.upper().endswith("CRS84") or text.upper() == "CRS:84":
        return 4326
    if match := EPSG.search(text):
        return int(match.group(1))
    return int(text) if text.isdigit() else None


def finite_bbox(xmin, ymin, xmax, ymax) -> tuple[float, float, float, float] | None:
    try:
        box = tuple(float(v) for v in (xmin, ymin, xmax, ymax))
    except (TypeError, ValueError):
        return None
    if any(v != v or v in (float("inf"), float("-inf")) for v in box):  # NaN check: v != v
        return None
    return box  # type: ignore[return-value]


def spatial_bbox(value) -> tuple[float, float, float, float] | None:
    """Bounding box from a DCAT/CKAN `spatial` value: "xmin,ymin,xmax,ymax" or GeoJSON."""
    if not value:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("{"):
            parts = [p for p in re.split(r"[,\s]+", text) if p]
            return finite_bbox(*parts) if len(parts) == 4 else None
        try:
            value = json.loads(text)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    points = list(_points(value.get("coordinates")))
    if not points:
        box = value.get("bbox")
        return finite_bbox(*box) if isinstance(box, list) and len(box) == 4 else None
    xs, ys = zip(*points, strict=True)
    return finite_bbox(min(xs), min(ys), max(xs), max(ys))


def _points(coords):
    if not isinstance(coords, list | tuple):
        return
    if len(coords) >= 2 and all(isinstance(v, int | float) for v in coords[:2]):
        yield coords[0], coords[1]
        return
    for part in coords:
        yield from _points(part)


def trim(text, limit: int = 4000) -> str | None:
    if text is None:
        return None
    text = str(text).strip()
    return text[:limit] or None


def compact(d: dict) -> dict:
    """Drop empty values so they don't add noise to stored records or content hashes."""
    return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != {}}
