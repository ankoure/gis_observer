"""Stages wired to the store: crawl -> classify -> probe -> harvest, plus health checks.

seeds ──> crawl ─┐
govsearch ───────┼─> candidates ─(classify, offline)─> endpoints ─> harvest ─> assets
imports ─────────┘        ▲              │ unknown                     │
                          │              └──> probe (network) ─────────┤
                          └──────────── candidates found in catalogs ──┘
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .classify import classify
from .discovery.crawl import crawl_site
from .discovery.probe import probe
from .harvesters import Harvester
from .http import Fetcher
from .models import Asset, Candidate, Endpoint, Platform
from .raw import RawCapture, raw_path
from .store import Store

log = logging.getLogger(__name__)


def ingest_candidates(store: Store, candidates: Iterable[Candidate]) -> tuple[int, int]:
    """Save candidates; any the classifier recognises become endpoints straight away.

    Already-known URLs are classified again so a second jurisdiction linking to the
    same server still gets recorded against it.
    """
    new = classified = 0
    for cand in candidates:
        new += store.add_candidate(cand)
        ep = classify(cand.url, cand.link_text)
        if ep is None:
            continue
        store.upsert_endpoint(ep, via=cand.via, found_on=cand.found_on or cand.url)
        if cand.jurisdiction_id:
            store.link_jurisdiction(ep, cand.jurisdiction_id, cand.found_on or cand.url)
        store.set_candidate_status(cand.url, "classified", ep.id)
        classified += 1
    store.commit()
    return new, classified


async def _bounded(items, worker, concurrency: int):
    """Run worker over items with at most `concurrency` in flight; yield results as they finish."""
    sem = asyncio.Semaphore(concurrency)

    async def run(item):
        async with sem:
            return item, await worker(item)

    for fut in asyncio.as_completed([run(i) for i in items]):
        yield await fut


async def run_crawl(
    store: Store, fetcher: Fetcher, *, limit=None, recrawl=False, max_pages=40, max_depth=2,
    concurrency=8,
) -> None:  # fmt: skip
    sites = store.jurisdictions_to_crawl(limit, recrawl)
    log.info("crawling %d jurisdiction websites", len(sites))

    async def crawl(j):
        try:
            return await crawl_site(fetcher, j, max_pages=max_pages, max_depth=max_depth)
        except Exception:
            log.exception("crawl failed: %s", j.website)
            return []

    async for j, cands in _bounded(sites, crawl, concurrency):
        new, classified = ingest_candidates(store, cands)
        store.mark_crawled(j.id)
        store.commit()
        log.info("%s: %d candidates (%d new, %d recognised)", j.name, len(cands), new, classified)


async def run_probe(store: Store, fetcher: Fetcher, *, limit=None, concurrency=8) -> None:
    rows = store.candidates_with_status("new", limit)
    log.info("probing %d unrecognised candidates", len(rows))

    async def check(row):
        try:
            return await probe(fetcher, row["url"], row["link_text"])
        except Exception:
            log.exception("probe failed: %s", row["url"])
            return None

    async for row, result in _bounded(rows, check, concurrency):
        if result is None:
            continue
        for ep in result.endpoints:
            store.upsert_endpoint(ep, via="probe", found_on=row["url"])
            if row["jurisdiction_id"]:
                store.link_jurisdiction(ep, row["jurisdiction_id"], row["url"])
        first = result.endpoints[0].id if result.endpoints else None
        store.set_candidate_status(row["url"], result.status, first)
        store.commit()
        if result.endpoints:
            log.info(
                "%s -> %s", row["url"], ", ".join(f"{e.platform}:{e.url}" for e in result.endpoints)
            )


async def run_harvest(
    store: Store, fetcher: Fetcher, raw_dir: Path, *, platforms: list[Platform] | None = None,
    ids: list[str] | None = None, older_than_days: float | None = 7, limit=None, concurrency=6,
) -> None:  # fmt: skip
    harvestable = [p for p in (platforms or Harvester.platforms()) if p in Harvester.platforms()]
    cutoff = None
    if older_than_days and older_than_days > 0 and not ids:  # 0 means harvest everything
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    endpoints = store.endpoints(platforms=harvestable, ids=ids, older_than=cutoff, limit=limit)
    log.info("harvesting %d endpoints", len(endpoints))
    async for _ep, _ in _bounded(
        endpoints, lambda ep: harvest_one(store, fetcher, raw_dir, ep), concurrency
    ):
        pass


async def harvest_one(store: Store, fetcher: Fetcher, raw_dir: Path, endpoint: Endpoint) -> str:
    capture = RawCapture(fetcher)
    harvester = Harvester.for_platform(endpoint.platform, capture)
    if harvester is None:
        return "unsupported"
    harvest_id = store.start_harvest(endpoint.id)
    assets: list[Asset] = []
    found: list[Candidate] = []
    try:
        async for item in harvester.harvest(endpoint):
            (assets if isinstance(item, Asset) else found).append(item)
    except Exception as exc:  # a parser bug shouldn't take the whole run down
        log.exception("harvester crashed on %s", endpoint.url)
        harvester.errors.append(f"crash: {exc!r}")

    errors = harvester.errors
    status = "ok" if not errors else ("partial" if assets else "failed")
    path = capture.write(raw_path(raw_dir, endpoint))
    counts = store.apply_assets(endpoint.id, harvest_id, assets, complete=status == "ok")
    ingest_candidates(store, found)
    store.finish_harvest(
        harvest_id, endpoint.id, status=status, counts=counts, n_assets=len(assets),
        n_candidates=len(found), errors=errors, raw_path=str(path) if path else None,
    )  # fmt: skip
    log.info(
        "%s %s: %s, %d assets (+%d new, %d changed, %d removed), %d leads%s",
        endpoint.platform, endpoint.url, status, len(assets), counts["new"], counts["changed"],
        counts["removed"], len(found), f", {len(errors)} errors" if errors else "",
    )  # fmt: skip
    return status


# Cheapest request that shows each kind of endpoint is up. Ported from the Munger's
# server_check.py / health_check.py, which did this for ArcGIS servers only.
def _check_request(ep: Endpoint) -> tuple[str, dict | None, int]:
    if ep.platform is Platform.ARCGIS_REST:
        return ep.url, {"f": "json"}, 5_000_000
    if ep.platform is Platform.ARCGIS_PORTAL:
        return f"{ep.url}/sharing/rest/portals/self", {"f": "json"}, 5_000_000
    if ep.platform is Platform.CKAN:
        return f"{ep.url}/api/3/action/status_show", None, 1_000_000
    if ep.platform is Platform.OGC:
        return ep.url, {"service": "WMS", "request": "GetCapabilities"}, 0
    return ep.url, None, 0


async def run_check(
    store: Store, fetcher: Fetcher, *, platforms=None, limit=None, concurrency=8
) -> None:
    endpoints = store.endpoints(platforms=platforms, limit=limit)

    async def check(ep):
        url, params, max_bytes = _check_request(ep)
        return await fetcher.get(url, params=params, max_bytes=max_bytes)

    async for ep, resp in _bounded(endpoints, check, concurrency):
        data = resp.json() if resp.content else None
        detail = None
        if isinstance(data, dict):
            detail = str(
                data.get("currentVersion") or data.get("ckan_version") or data.get("id") or ""
            )
            if "error" in data:
                detail = f"error {data['error']}"
        store.record_check(ep.id, resp, detail or resp.error)
        store.commit()
        log.info("%s %s -> %s %s", ep.platform, ep.url, resp.status, detail or resp.error or "")
