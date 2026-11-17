import asyncio
from datetime import UTC, datetime

import httpx
from helpers import make_fetcher

from gis_observer import cli, pipeline
from gis_observer.models import Endpoint, Platform
from gis_observer.raw import RawCapture, ReplayFetcher, raw_path
from gis_observer.store import Store

FILE = Endpoint(Platform.FILE, "https://gis.town.gov/download/parcels.zip")
DAY1 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
DAY2 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def _capture(raw_dir, endpoint, when, routes, requests):
    """Write a raw file as a live harvest would have, dated `when`."""

    async def go():
        async with make_fetcher(routes) as http:
            cap = RawCapture(http)
            for method, url, params in requests:
                if method == "HEAD":
                    await cap.head(url)
                else:
                    await cap.get(url, params=params)
            return cap

    return asyncio.run(go()).write(raw_path(raw_dir, endpoint, when))


def _head(etag):
    return httpx.Response(200, headers={"etag": etag, "content-length": "10"})


def test_replay_fetcher_matches_requests(tmp_path):
    url = "https://gis.town.gov/wms"
    path = _capture(
        tmp_path, FILE, DAY1, {url: lambda req: {"q": str(req.url.params)}},
        [("GET", url, {"service": "WMS", "request": "GetCapabilities"}), ("GET", url, None)],
    )  # fmt: skip
    replay = ReplayFetcher(path)

    async def go():
        # params match whatever their order; GET and HEAD are different requests
        a = await replay.get(url, params={"request": "GetCapabilities", "service": "WMS"})
        b = await replay.get(url)
        again = await replay.get(url)
        head = await replay.head(url)
        return a, b, again, head

    a, b, again, head = asyncio.run(go())
    assert a.json() == {"q": "service=WMS&request=GetCapabilities"}
    assert b.json() == again.json() == {"q": ""}
    assert (head.status, head.error) == (None, ReplayFetcher.MISSING)
    assert replay.misses == [f"HEAD {url}"]
    assert replay.finished_at == "2026-11-16T09:00:00+00:00"


def test_replay_rebuilds_history_with_original_dates(tmp_path):
    raw = tmp_path / "raw"
    _capture(raw, FILE, DAY1, {FILE.url: _head('"a"')}, [("HEAD", FILE.url, None)])
    _capture(raw, FILE, DAY2, {FILE.url: _head('"b"')}, [("HEAD", FILE.url, None)])

    store = Store(":memory:")
    store.upsert_endpoint(FILE)
    stale = store.start_harvest(FILE.id)  # history from an older parser, to be replaced
    store.apply_assets(FILE.id, stale, [], complete=True)

    totals = asyncio.run(pipeline.run_replay(store, raw))
    assert totals == {"endpoints": 1, "files": 2, "skipped": 0}

    harvests = store.query("SELECT status, finished_at, n_new, n_changed FROM harvests ORDER BY id")
    assert [tuple(h) for h in harvests] == [
        ("ok", "2026-11-16T09:00:00+00:00", 1, 0),
        ("ok", "2026-11-17T09:00:00+00:00", 0, 1),
    ]
    versions = store.query("SELECT change, observed_at FROM asset_versions ORDER BY observed_at")
    assert [tuple(v) for v in versions] == [
        ("new", "2026-11-16T09:00:00+00:00"),
        ("changed", "2026-11-17T09:00:00+00:00"),
    ]
    asset = store.query("SELECT first_seen, last_seen FROM assets")[0]
    assert tuple(asset) == ("2026-11-16T09:00:00+00:00", "2026-11-17T09:00:00+00:00")
    ep = store.query("SELECT last_harvest_at FROM endpoints")[0]
    assert ep["last_harvest_at"] == "2026-11-17T09:00:00+00:00"

    # --since starts history at that date
    asyncio.run(pipeline.run_replay(store, raw, since=DAY2))
    versions = store.query("SELECT change, observed_at FROM asset_versions")
    assert [tuple(v) for v in versions] == [("new", "2026-11-17T09:00:00+00:00")]


def test_request_missing_from_raw_file_makes_harvest_incomplete(tmp_path):
    # captured when the server refused HEAD and the harvester stopped there; a harvester
    # that now falls back to GET asks for something the file doesn't have
    refused = httpx.Response(405)
    _capture(tmp_path, FILE, DAY1, {FILE.url: refused}, [("HEAD", FILE.url, None)])
    store = Store(":memory:")
    store.upsert_endpoint(FILE)
    asyncio.run(pipeline.run_replay(store, tmp_path))
    harvest = store.query("SELECT status, errors FROM harvests")[0]
    assert harvest["status"] == "failed"
    assert ReplayFetcher.MISSING in harvest["errors"]


def test_replay_command_writes_a_copy(tmp_path):
    db, out, raw = tmp_path / "live.sqlite", tmp_path / "replayed.sqlite", tmp_path / "raw"
    _capture(raw, FILE, DAY1, {FILE.url: _head('"a"')}, [("HEAD", FILE.url, None)])
    live = Store(db)
    live.upsert_endpoint(FILE)
    live.close()

    argv = ["--db", str(db), "--raw-dir", str(raw), "replay", "--out", str(out)]
    assert cli.main(argv) == 0
    assert Store(out).query("SELECT COUNT(*) FROM harvests")[0][0] == 1
    assert Store(db).query("SELECT COUNT(*) FROM harvests")[0][0] == 0  # live catalog untouched
    assert cli.main(argv) == 2  # never overwrites
