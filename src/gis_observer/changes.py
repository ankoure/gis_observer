"""What changed on the catalogued servers: asset history and up/down checks, by endpoint.

Answers "what changed on these servers this month?" from `asset_versions` and `checks`.
An endpoint's first harvest is one summary line rather than a `new` row per asset.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .store import Store
from .util import parse_when

SPAN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([hdw])\s*$", re.I)
UNITS = {"h": "hours", "d": "days", "w": "weeks"}
# Snapshot keys shown under friendlier names in a list of differences.
LABELS = {"bbox": "extent", "bbox_srid": "extent_srid"}


def parse_since(text: str, now: datetime | None = None) -> datetime | None:
    """`30d`, `12h`, `2w`, or a date/time (ISO, UTC) -> aware UTC datetime."""
    if m := SPAN.match(text or ""):
        return (now or datetime.now(UTC)) - timedelta(**{UNITS[m[2].lower()]: float(m[1])})
    return parse_when(text)


def changed_fields(before: str | None, after: str | None) -> list[str]:
    """Names of the snapshot fields that differ; `extra`'s entries count as fields."""
    if not before or not after:
        return []
    old, new = _flatten(json.loads(before)), _flatten(json.loads(after))
    return [LABELS.get(k, k) for k in sorted(old.keys() | new.keys()) if old.get(k) != new.get(k)]


def _flatten(snapshot: dict) -> dict:
    out = {k: v for k, v in snapshot.items() if k != "extra"}
    for k, v in (snapshot.get("extra") or {}).items():
        out[k if k not in out else f"extra.{k}"] = v
    return out


@dataclass
class Health:
    """One endpoint's checks in the window, when any of them failed."""

    endpoint_id: str
    platform: str
    endpoint_url: str
    state: str  # down | recovered
    checks: int
    failed: int
    latest_at: str
    http_status: int | None
    detail: str | None
    down_since: str | None
    last_up_at: str | None


@dataclass
class EndpointChanges:
    endpoint_id: str
    platform: str
    endpoint_url: str
    first_harvest: tuple[str, int] | None = None  # (observed_at, assets found)
    assets: list[dict] = field(default_factory=list)


def asset_changes(
    store: Store, since: str, *, ids=None, platforms=None, kinds=None
) -> list[EndpointChanges]:
    """Each endpoint's changes since `since`, ordered by endpoint URL."""
    by_endpoint: dict[str, EndpointChanges] = {}

    def entry(r) -> EndpointChanges:
        if r["endpoint_id"] not in by_endpoint:
            by_endpoint[r["endpoint_id"]] = EndpointChanges(
                r["endpoint_id"], r["platform"], r["endpoint_url"]
            )
        return by_endpoint[r["endpoint_id"]]

    filters = {"ids": ids, "platforms": platforms, "kinds": kinds}
    for r in store.first_harvests(since, **filters):
        entry(r).first_harvest = (r["observed_at"], r["n"])
    for r in store.asset_changes(since, **filters):
        entry(r).assets.append(
            {
                "observed_at": r["observed_at"],
                "change": r["change"],
                "kind": r["kind"],
                "name": r["name"],
                "key": r["key"],
                "fields": changed_fields(r["prev_snapshot"], r["snapshot"])
                if r["change"] == "changed"
                else [],
            }
        )
    return sorted(by_endpoint.values(), key=lambda e: (e.endpoint_url, e.endpoint_id))


def health(store: Store, since: str, *, ids=None, platforms=None) -> list[Health]:
    """Endpoints whose latest check failed, or that failed and came back, since `since`."""
    groups: dict[str, list] = {}
    for r in store.checks_since(since, ids=ids, platforms=platforms):
        groups.setdefault(r["endpoint_id"], []).append(r)
    out = []
    for rows in groups.values():
        latest = rows[-1]
        failed = sum(1 for r in rows if not r["ok"])
        if not failed:
            continue
        out.append(
            Health(
                endpoint_id=latest["endpoint_id"],
                platform=latest["platform"],
                endpoint_url=latest["endpoint_url"],
                state="recovered" if latest["ok"] else "down",
                checks=len(rows),
                failed=failed,
                latest_at=latest["checked_at"],
                http_status=latest["http_status"],
                detail=latest["detail"],
                down_since=latest["down_since"],
                last_up_at=latest["last_up_at"],
            )
        )
    return sorted(out, key=lambda h: (h.state != "down", h.endpoint_url))
