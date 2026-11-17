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
from .util import parse_when

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
    s.add_argument("--state", help="osm: the seeds' two-letter state, e.g. MA")
    s.add_argument("--kind", dest="jkind", help="osm: what the seeds are, e.g. Municipality")
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
    h.add_argument("--concurrency", type=int, default=6)
    h.set_defaults(func=cmd_harvest)

    rp = sub.add_parser(
        "replay",
        help="rebuild harvest history from raw files into a copy of the catalog",
        description=(
            "Copy the catalog to --out, then re-run today's harvesters over the raw files, "
            "oldest first, with each harvest keeping its original time. Each replayed "
            "endpoint's harvests, assets and history are rebuilt from scratch; nothing is "
            "fetched and --db is not changed. Check the copy, then swap it in."
        ),
    )
    rp.add_argument("--out", type=Path, required=True, help="new catalog to write (must not exist)")
    rp.add_argument("--endpoint", nargs="*", default=None, help="only these endpoint ids")
    rp.add_argument("--since", help="only raw files written on or after this date/time (ISO, UTC)")
    rp.set_defaults(func=cmd_replay)

    k = sub.add_parser("check", help="record whether endpoints are up")
    k.add_argument("--platform", nargs="*", choices=PLATFORMS, default=None)
    k.add_argument("--limit", type=int)
    k.set_defaults(func=cmd_check)

    a = sub.add_parser(
        "attribute", help="credit endpoints to the jurisdictions they're named for or found under"
    )
    a.set_defaults(func=cmd_attribute)

    r = sub.add_parser("report", help="summarise the catalog")
    r.set_defaults(func=cmd_report)
    return p


def _fetcher(args) -> Fetcher:
    return Fetcher(user_agent=args.user_agent)


def cmd_seeds(args, store: Store) -> None:
    if args.kind == "osm":
        rows = seeds.load_osm_seeds(args.file, state=args.state, kind=args.jkind)
        n = store.upsert_jurisdictions(rows)
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
                concurrency=args.concurrency,
            )  # fmt: skip

    asyncio.run(go())


def cmd_replay(args, store: Store) -> int:
    since = None
    if args.since:
        since = parse_when(args.since)
        if since is None:
            print(f"can't read --since {args.since!r}; use a date like 2026-11-16")
            return 2
    if args.out.exists() or args.out.resolve() == args.db.resolve():
        print(f"{args.out} already exists; replay only writes a new catalog")
        return 2
    store.copy_to(args.out)
    out = Store(args.out)
    try:
        totals = asyncio.run(pipeline.run_replay(out, args.raw_dir, ids=args.endpoint, since=since))
    finally:
        out.close()
    print(
        f"replayed {totals['files']} raw files for {totals['endpoints']} endpoints into "
        f"{args.out}"
        + (f" ({totals['skipped']} unreadable files skipped)" if totals["skipped"] else "")
    )
    return 0


def cmd_check(args, store: Store) -> None:
    platforms = [Platform(p) for p in args.platform] if args.platform else None

    async def go():
        async with _fetcher(args) as f:
            await pipeline.run_check(store, f, platforms=platforms, limit=args.limit)

    asyncio.run(go())


def cmd_attribute(args, store: Store) -> None:
    changes = store.attribute_all()
    print(", ".join(f"{rel} {n:+d}" for rel, n in changes.items()))


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
        "coverage: what each jurisdiction's discovery found",
        [tuple(r) for r in store.coverage()],
        ["state", "total", "own endpoint", "linked only", "crawl failed", "nothing found",
         "not crawled"],
    )  # fmt: skip
    _table(
        "crawl failures",
        q("""SELECT crawl_failure, COUNT(*) FROM jurisdictions
             WHERE crawl_failure IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"""),
        ["reason", "n"],
    )
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
