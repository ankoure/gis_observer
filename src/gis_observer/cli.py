"""Command line: gis-observer <command>. Run with --help for the list."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from . import pipeline
from .discovery import seeds
from .discovery.govsearch import read_gis_links
from .http import DEFAULT_USER_AGENT, Fetcher
from .models import Platform
from .store import Store

DEFAULT_DB = Path(os.environ.get("GIS_OBSERVER_DB", "data/observer.sqlite"))
DEFAULT_RAW = Path(os.environ.get("GIS_OBSERVER_RAW", "data/raw"))
PLATFORMS = [p.value for p in Platform]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = Store(args.db)
    try:
        return args.func(args, store) or 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gis-observer", description=__doc__)
    p.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="SQLite catalog (default %(default)s)"
    )
    p.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW, help="raw response landing zone")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(required=True, metavar="command")

    s = sub.add_parser("seeds", help="load jurisdictions, known servers, or URL lists")
    s.add_argument("kind", choices=["osm", "dotgov", "arcgis-servers", "urls"])
    s.add_argument("file", type=Path)
    s.add_argument("--types", nargs="*", default=[], help="dotgov: Domain types, e.g. City County")
    s.add_argument("--states", nargs="*", default=[], help="dotgov: two-letter states")
    s.set_defaults(func=cmd_seeds)

    c = sub.add_parser("crawl", help="crawl jurisdiction websites for GIS links")
    c.add_argument("--limit", type=int)
    c.add_argument("--recrawl", action="store_true", help="include sites crawled before")
    c.add_argument("--max-pages", type=int, default=40)
    c.add_argument("--max-depth", type=int, default=2)
    c.add_argument("--concurrency", type=int, default=8)
    c.set_defaults(func=cmd_crawl)

    g = sub.add_parser("govsearch", help="import gis_links from a govsearch OpenSearch index")
    g.add_argument("--url", default=os.environ.get("OPENSEARCH_URL", "http://localhost:9200"))
    g.add_argument("--index", default=os.environ.get("OPENSEARCH_INDEX", "govsites"))
    g.set_defaults(func=cmd_govsearch)

    pr = sub.add_parser("probe", help="identify candidates the classifier couldn't")
    pr.add_argument("--limit", type=int)
    pr.add_argument("--concurrency", type=int, default=8)
    pr.set_defaults(func=cmd_probe)

    h = sub.add_parser("harvest", help="catalog endpoints' services, layers and datasets")
    h.add_argument("--platform", nargs="*", choices=PLATFORMS, default=None)
    h.add_argument(
        "--endpoint", nargs="*", default=None, help="endpoint ids (ignores --older-than)"
    )
    h.add_argument(
        "--older-than-days",
        type=float,
        default=7,
        help="skip endpoints harvested more recently than this; 0 harvests all (default %(default)s)",
    )
    h.add_argument("--limit", type=int)
    h.add_argument(
        "--linked-only",
        action="store_true",
        help="only endpoints a jurisdiction links to (they come first anyway)",
    )
    h.add_argument("--concurrency", type=int, default=6)
    h.set_defaults(func=cmd_harvest)

    cy = sub.add_parser("cycle", help="probe, harvest, probe the leads, harvest those, then check")
    cy.add_argument("--probe-limit", type=int, help="candidates per probe stage")
    cy.add_argument("--harvest-limit", type=int, help="due endpoints to harvest")
    cy.add_argument("--lead-harvest-limit", type=int, help="endpoints found this cycle to harvest")
    cy.add_argument("--check-limit", type=int, help="endpoints to check")
    cy.add_argument("--older-than-days", type=float, default=7)
    cy.add_argument("--linked-only", action="store_true")
    cy.add_argument("--no-check", action="store_true", help="skip the check stage")
    cy.set_defaults(func=cmd_cycle)

    k = sub.add_parser("check", help="record whether endpoints are up")
    k.add_argument("--platform", nargs="*", choices=PLATFORMS, default=None)
    k.add_argument("--limit", type=int)
    k.set_defaults(func=cmd_check)

    r = sub.add_parser("report", help="summarise the catalog")
    r.set_defaults(func=cmd_report)
    return p


def _fetcher(args) -> Fetcher:
    return Fetcher(user_agent=args.user_agent)


def cmd_seeds(args, store: Store) -> None:
    if args.kind == "osm":
        n = store.upsert_jurisdictions(seeds.load_osm_seeds(args.file))
        print(f"{n} jurisdictions loaded")
    elif args.kind == "dotgov":
        rows = seeds.load_dotgov(args.file, types=args.types, states=args.states)
        print(f"{store.upsert_jurisdictions(rows)} jurisdictions loaded")
    elif args.kind == "arcgis-servers":
        eps = seeds.load_arcgis_servers(args.file)
        for ep in eps:
            store.upsert_endpoint(ep, via=f"import:{args.file.name}", found_on=None)
        store.commit()
        print(f"{len(eps)} ArcGIS servers loaded")
    else:
        new, classified = pipeline.ingest_candidates(store, seeds.load_url_list(args.file))
        print(f"{new} new candidates, {classified} recognised")


def cmd_crawl(args, store: Store) -> None:
    async def go():
        async with _fetcher(args) as f:
            await pipeline.run_crawl(
                store, f, limit=args.limit, recrawl=args.recrawl, max_pages=args.max_pages,
                max_depth=args.max_depth, concurrency=args.concurrency,
            )  # fmt: skip

    asyncio.run(go())


def cmd_govsearch(args, store: Store) -> None:
    async def go():
        cands = [c async for c in read_gis_links(args.url, args.index)]
        new, classified = pipeline.ingest_candidates(store, cands)
        print(f"{len(cands)} links read, {new} new candidates, {classified} recognised")

    asyncio.run(go())


def cmd_probe(args, store: Store) -> None:
    async def go():
        async with _fetcher(args) as f:
            await pipeline.run_probe(store, f, limit=args.limit, concurrency=args.concurrency)

    asyncio.run(go())


def cmd_harvest(args, store: Store) -> None:
    platforms = [Platform(p) for p in args.platform] if args.platform else None

    async def go():
        async with _fetcher(args) as f:
            await pipeline.run_harvest(
                store, f, args.raw_dir, platforms=platforms, ids=args.endpoint,
                older_than_days=args.older_than_days, limit=args.limit,
                concurrency=args.concurrency, linked_only=args.linked_only,
            )  # fmt: skip

    asyncio.run(go())


def cmd_cycle(args, store: Store) -> None:
    async def go():
        async with _fetcher(args) as f:
            return await pipeline.run_cycle(
                store, f, args.raw_dir, probe_limit=args.probe_limit,
                harvest_limit=args.harvest_limit, lead_harvest_limit=args.lead_harvest_limit,
                check_limit=args.check_limit, older_than_days=args.older_than_days,
                linked_only=args.linked_only, skip_check=args.no_check,
            )  # fmt: skip

    print_cycle_summary(asyncio.run(go()))


# Columns per stage kind, in display order; anything else a tally holds is appended.
_SUMMARY_KEYS = {
    "probe": ["candidates", "found", "none", "dead", "error", "endpoints"],
    "harvest": ["endpoints", "ok", "partial", "failed", "new", "changed", "removed",
                "returned", "leads"],
    "check": ["endpoints", "up", "down"],
}  # fmt: skip


def print_cycle_summary(stages: dict) -> None:
    print("\ncycle summary")
    for stage, tally in stages.items():
        keys = _SUMMARY_KEYS[stage.split()[0]]
        keys = keys + sorted(k for k in tally if k not in keys)
        print(f"  {stage:<12} " + ", ".join(f"{k} {tally.get(k, 0)}" for k in keys))


def cmd_check(args, store: Store) -> None:
    platforms = [Platform(p) for p in args.platform] if args.platform else None

    async def go():
        async with _fetcher(args) as f:
            await pipeline.run_check(store, f, platforms=platforms, limit=args.limit)

    asyncio.run(go())


def _table(title: str, rows, headers: list[str]) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    data = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in data)) for i, h in enumerate(headers)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    for r in data:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)))


def cmd_report(args, store: Store) -> None:
    q = store.query
    j = q("SELECT COUNT(*), COUNT(last_crawled_at) FROM jurisdictions")[0]
    print(f"jurisdictions: {j[0]} ({j[1]} crawled)")
    _table(
        "candidates by status",
        q("SELECT status, COUNT(*) FROM candidates GROUP BY 1 ORDER BY 2 DESC"),
        ["status", "n"],
    )
    _table(
        "endpoints by platform",
        q("""SELECT platform, COUNT(*), COUNT(last_harvest_at),
                    SUM(last_harvest_status = 'ok'), SUM(last_harvest_status != 'ok')
             FROM endpoints GROUP BY 1 ORDER BY 2 DESC"""),
        ["platform", "endpoints", "harvested", "ok", "failed/partial"],
    )
    _table(
        "viewer vendors",
        q("""SELECT vendor, COUNT(*) FROM endpoints WHERE platform = 'viewer'
             GROUP BY 1 ORDER BY 2 DESC"""),
        ["vendor", "n"],
    )
    _table(
        "current assets",
        q("""SELECT platform, kind, COUNT(*) FROM current_assets
             GROUP BY 1, 2 ORDER BY 1, 3 DESC"""),
        ["platform", "kind", "n"],
    )
    _table(
        "largest endpoints (layers, tables, datasets, files)",
        q("""SELECT platform, endpoint_url, COUNT(*) FROM current_assets
             WHERE kind IN ('layer', 'table', 'raster', 'dataset', 'file')
             GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10"""),
        ["platform", "endpoint", "n"],
    )
    _table(
        "changes, last 7 days",
        q("""SELECT change, COUNT(*) FROM asset_versions
             WHERE observed_at >= datetime('now', '-7 days') GROUP BY 1 ORDER BY 2 DESC"""),
        ["change", "n"],
    )
