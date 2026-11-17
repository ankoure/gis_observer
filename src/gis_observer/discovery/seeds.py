"""Loaders for the starting lists that discovery works from."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from ..classify import classify
from ..models import Candidate, Endpoint, Jurisdiction, Platform


def load_osm_seeds(
    path: Path, *, state: str | None = None, kind: str | None = None
) -> list[Jurisdiction]:
    """govsearch's seeds.csv: `@id,source_name,website`, one row per OSM boundary relation.

    The file doesn't say which state the towns are in or what they are, so the caller
    does (`state="MA", kind="Municipality"`).
    """
    state = state.strip().upper() if state else None
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            osm_id, name = (row.get("@id") or "").strip(), (row.get("source_name") or "").strip()
            if not osm_id or not name:
                continue
            out.append(
                Jurisdiction(
                    id=f"osm:{osm_id}",
                    name=name,
                    website=_site(row.get("website")),
                    kind=kind or None,
                    state=state,
                    source="govsearch-seeds",
                )
            )
    return out


def load_dotgov(
    path: Path, *, types: Iterable[str] = (), states: Iterable[str] = ()
) -> list[Jurisdiction]:
    """CISA's registered .gov domains (github.com/cisagov/dotgov-data, current-full.csv).

    This is the list the 2024 Munger joined against Common Crawl.
    """
    want_types = {t.lower() for t in types}
    want_states = {s.upper() for s in states}
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            domain = (row.get("Domain name") or row.get("Domain Name") or "").strip().lower()
            kind = (row.get("Domain type") or row.get("Domain Type") or "").strip()
            state = (row.get("State") or "").strip().upper()
            if not domain or (want_types and kind.lower() not in want_types):
                continue
            if want_states and state not in want_states:
                continue
            org = (row.get("Organization name") or row.get("Organization") or domain).strip()
            sub = (row.get("Suborganization name") or "").strip()
            out.append(
                Jurisdiction(
                    id=f"dotgov:{domain}",
                    name=f"{org} - {sub}" if sub else org,
                    website=f"https://{domain}",
                    kind=kind or None,
                    state=state or None,
                    source="cisa-dotgov",
                )
            )
    return out


def load_arcgis_servers(path: Path) -> list[Endpoint]:
    """The Munger's servers table export: `server_uuid,data_owner,comment,url`."""
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ep = classify((row.get("url") or "").strip())
            if ep is None or ep.platform is not Platform.ARCGIS_REST:
                continue
            owner = (row.get("data_owner") or "").strip() or None
            out.append(Endpoint(ep.platform, ep.url, label=owner))
    return out


def load_url_list(path: Path) -> list[Candidate]:
    """A CSV with a `url` column (e.g. Athena results), or a plain list of URLs."""
    path = Path(path)
    via = f"import:{path.name}"
    with open(path, newline="", encoding="utf-8") as fh:
        first = fh.readline()
        fh.seek(0)
        if "url" in [c.strip().lower() for c in first.split(",")]:
            rows = csv.DictReader(fh)
            col = next(c for c in rows.fieldnames or [] if c.strip().lower() == "url")
            urls = [(r.get(col) or "").strip() for r in rows]
        else:
            urls = [line.strip() for line in fh]
    return [Candidate(url=u, via=via) for u in urls if u.startswith(("http://", "https://"))]


def _site(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    return value if "://" in value else f"https://{value}"
