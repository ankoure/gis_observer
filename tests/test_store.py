import asyncio

from helpers import make_fetcher

from gis_observer import pipeline
from gis_observer.models import Asset, Candidate, Endpoint, Jurisdiction, Platform
from gis_observer.store import Store

EP = Endpoint(Platform.ARCGIS_REST, "https://gis.town.gov/arcgis/rest/services")


def _apply(store, assets, complete=True):
    hid = store.start_harvest(EP.id)
    return store.apply_assets(EP.id, hid, assets, complete=complete)


def test_change_tracking():
    store = Store(":memory:")
    store.upsert_endpoint(EP)
    a = Asset(key="a", kind="layer", name="Parcels")
    b = Asset(key="b", kind="layer", name="Zoning")
    assert _apply(store, [a, b]) == {"new": 2, "changed": 0, "removed": 0, "returned": 0}
    assert _apply(store, [a, b]) == {"new": 0, "changed": 0, "removed": 0, "returned": 0}

    a2 = Asset(key="a", kind="layer", name="Parcels", description="now with owners")
    assert _apply(store, [a2]) == {"new": 0, "changed": 1, "removed": 0, "returned": 0}
    assert _apply(store, [a2]) == {"new": 0, "changed": 0, "removed": 1, "returned": 0}
    assert _apply(store, [a2, b]) == {"new": 0, "changed": 0, "removed": 0, "returned": 1}

    history = store.query("SELECT key, change FROM asset_versions ORDER BY harvest_id, key")
    assert [tuple(r) for r in history] == [
        ("a", "new"), ("b", "new"), ("a", "changed"), ("b", "removed"), ("b", "returned"),
    ]  # fmt: skip
    live = store.query("SELECT key FROM current_assets ORDER BY key")
    assert [r["key"] for r in live] == ["a", "b"]


def _missed(store):
    return {r["key"]: r["missed_at"] for r in store.query("SELECT key, missed_at FROM assets")}


def test_one_complete_miss_is_not_a_removal():
    store = Store(":memory:")
    store.upsert_endpoint(EP)
    a, b = Asset(key="a", kind="layer"), Asset(key="b", kind="layer")
    _apply(store, [a, b])
    # An out-of-sync node leaves b out once, then it's back: no history either way.
    assert _apply(store, [a])["removed"] == 0
    assert _missed(store)["b"] is not None
    assert _apply(store, [a, b]) == {"new": 0, "changed": 0, "removed": 0, "returned": 0}
    assert _missed(store) == {"a": None, "b": None}
    assert [r["change"] for r in store.query("SELECT change FROM asset_versions")] == ["new"] * 2
    assert store.query("SELECT COUNT(*) FROM current_assets")[0][0] == 2

    # A fresh miss after the miss was cleared starts counting again.
    assert _apply(store, [a])["removed"] == 0
    assert _apply(store, [a])["removed"] == 1
    assert _missed(store) == {"a": None, "b": None}
    gone = store.query("SELECT key FROM assets WHERE removed_at IS NOT NULL")
    assert [r["key"] for r in gone] == ["b"]


def test_partial_harvest_leaves_misses_alone():
    store = Store(":memory:")
    store.upsert_endpoint(EP)
    a, b, c = (Asset(key=k, kind="layer") for k in "abc")
    _apply(store, [a, b, c])
    _apply(store, [a, b])  # c missed once
    # A partial harvest neither records b's miss nor clears c's.
    assert _apply(store, [a, c], complete=False)["removed"] == 0
    assert _missed(store)["b"] is None and _missed(store)["c"] is not None
    # So the next complete miss removes c but only records b's first miss.
    assert _apply(store, [a])["removed"] == 1
    assert _missed(store)["b"] is not None
    gone = store.query("SELECT key FROM assets WHERE removed_at IS NOT NULL")
    assert [r["key"] for r in gone] == ["c"]


def test_partial_harvest_never_removes():
    store = Store(":memory:")
    store.upsert_endpoint(EP)
    _apply(store, [Asset(key="a", kind="layer"), Asset(key="b", kind="layer")])
    counts = _apply(store, [Asset(key="a", kind="layer")], complete=False)
    assert counts["removed"] == 0
    assert store.query("SELECT COUNT(*) FROM current_assets")[0][0] == 2


def test_ingest_links_jurisdictions_by_domain():
    store = Store(":memory:")
    store.upsert_jurisdictions(
        [
            Jurisdiction("osm:1", "Town", "https://www.town.gov"),
            Jurisdiction("osm:2", "Other", "https://other.gov"),
        ]
    )
    cands = [
        Candidate(
            "https://gis.town.gov/arcgis/rest/services/X/MapServer",
            "crawl",
            jurisdiction_id="osm:1",
        ),
        Candidate("https://gis.town.gov/arcgis/rest/services", "crawl", jurisdiction_id="osm:2"),
        Candidate("https://county.gov/gis", "crawl", jurisdiction_id="osm:1"),
    ]
    new, classified = pipeline.ingest_candidates(store, cands)
    assert (new, classified) == (3, 2)
    rows = store.query("SELECT jurisdiction_id, relation FROM endpoint_jurisdictions ORDER BY 1")
    assert [tuple(r) for r in rows] == [("osm:1", "same_domain"), ("osm:2", "linked")]
    assert [r["url"] for r in store.candidates_with_status("new", None)] == [
        "https://county.gov/gis"
    ]


def test_harvest_one_end_to_end(tmp_path):
    store = Store(":memory:")
    portal = Endpoint(Platform.ARCGIS_PORTAL, "https://town.maps.arcgis.com")
    store.upsert_endpoint(portal)
    routes = {
        f"{portal.url}/sharing/rest/portals/self": {"id": "ORG1", "name": "Town"},
        f"{portal.url}/sharing/rest/search": {
            "nextStart": -1,
            "results": [{"id": "a" * 32, "type": "Feature Service", "title": "Parcels",
                         "url": "https://services1.arcgis.com/ORG1/arcgis/rest/services/P/FeatureServer"}],
        },
    }  # fmt: skip

    async def go():
        async with make_fetcher(routes) as http:
            return await pipeline.harvest_one(store, http, tmp_path, portal)

    assert asyncio.run(go()) == "ok"
    # the hosted feature server behind the item became an endpoint of its own
    platforms = {
        r["platform"]: r["url"] for r in store.query("SELECT platform, url FROM endpoints")
    }
    assert platforms["arcgis_rest"] == "https://services1.arcgis.com/ORG1/arcgis/rest/services"
    harvest = store.query("SELECT status, n_assets, n_new, raw_path FROM harvests")[0]
    assert (harvest["status"], harvest["n_assets"], harvest["n_new"]) == ("ok", 2, 2)
    assert harvest["raw_path"].endswith(".jsonl.gz")


def test_old_database_gains_crawl_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jurisdictions (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
               "website TEXT, kind TEXT, state TEXT, source TEXT, last_crawled_at TEXT)")  # fmt: skip
    db.execute("INSERT INTO jurisdictions (id, name) VALUES ('osm:1', 'Town')")
    db.commit()
    db.close()
    store = Store(path)
    store.mark_crawled("osm:1", pages=0, failure="blocked")
    row = store.query("SELECT crawl_pages, crawl_failure FROM jurisdictions")[0]
    assert tuple(row) == (0, "blocked")


def test_jurisdiction_assets_and_coverage():
    store = Store(":memory:")
    store.upsert_jurisdictions(
        [
            Jurisdiction("osm:1", "Own", "https://town.gov", "Municipality", "MA"),
            Jurisdiction("osm:2", "Linker", "https://linker.gov", "Municipality", "MA"),
            Jurisdiction("osm:3", "Empty", "https://empty.gov", "Municipality", "MA"),
            Jurisdiction("osm:4", "Blocked", "https://blocked.gov", "Municipality", "MA"),
            Jurisdiction("osm:5", "Later", "https://later.gov", "Municipality", "MA"),
        ]
    )
    # Reloading without state or kind keeps what was there.
    store.upsert_jurisdictions([Jurisdiction("osm:5", "Later", "https://later.gov")])
    store.upsert_endpoint(EP)
    store.link_jurisdiction(EP, "osm:1", "https://town.gov/gis")
    store.link_jurisdiction(EP, "osm:2", "https://linker.gov/maps")
    for jid in ("osm:1", "osm:2", "osm:3"):
        store.mark_crawled(jid, pages=5)
    store.mark_crawled("osm:4", pages=0, failure="blocked")
    _apply(store, [Asset(key="p", kind="layer", name="Parcels", format="esri-feature")])

    rows = store.query(
        """SELECT jurisdiction_name, relation, platform, endpoint_url, name, format
           FROM jurisdiction_assets WHERE state = 'MA' AND lower(name) LIKE '%parcel%'
           ORDER BY jurisdiction_name"""
    )
    assert [tuple(r) for r in rows] == [
        ("Linker", "linked", "arcgis_rest", EP.url, "Parcels", "esri-feature"),
        ("Own", "same_domain", "arcgis_rest", EP.url, "Parcels", "esri-feature"),
    ]
    cov = store.coverage()
    assert [dict(r) for r in cov] == [
        {"state": "MA", "total": 5, "own_domain": 1, "linked_only": 1, "crawl_failed": 1,
         "nothing_found": 1, "not_crawled": 1},
    ]  # fmt: skip


def test_imported_urls_are_attributed_by_host():
    store = Store(":memory:")
    store.upsert_jurisdictions(
        [
            Jurisdiction("osm:1", "Holland", "https://www.holland-ma.gov"),
            Jurisdiction("osm:2", "Twin A", "https://shared.org"),
            Jurisdiction("osm:3", "Twin B", "https://www.shared.org"),
        ]
    )
    cands = store.attribute_by_host(
        [
            Candidate("https://gis.holland-ma.gov/arcgis/rest/services", "import:cc.csv"),
            Candidate("https://holland-ma.gov/maps", "import:cc.csv"),
            Candidate("https://shared.org/gis", "import:cc.csv"),
            Candidate("https://notholland-ma.gov/gis", "import:cc.csv"),
            Candidate("https://holland-ma.gov/gis", "import:cc.csv", jurisdiction_id="osm:9"),
        ]
    )
    assert [c.jurisdiction_id for c in cands] == ["osm:1", "osm:1", None, None, "osm:9"]
    pipeline.ingest_candidates(store, cands)
    assert store.coverage()[0]["own_domain"] == 1


def test_old_viewer_ids_are_merged(tmp_path):
    from gis_observer.models import stable_id

    path = tmp_path / "old.sqlite"
    store = Store(path)
    store.upsert_jurisdictions([Jurisdiction("osm:1", "Hampden", "https://hampden.gov")])
    # As stored before viewer ids folded scheme, host alias and slug case.
    old = [
        ("https://www.axisgis.com/hampdenma", "2024-01-02"),
        ("https://next.axisgis.com/HampdenMA", "2024-01-01"),
    ]
    for url, seen in old:
        oid = stable_id(Platform.VIEWER, url)
        store.db.execute(
            """INSERT INTO endpoints (id, platform, url, vendor, first_seen)
               VALUES (?, 'viewer', ?, 'AxisGIS', ?)""",
            (oid, url, seen),
        )
        store.db.execute(
            """INSERT INTO endpoint_jurisdictions VALUES (?, 'osm:1', 'linked', ?)""",
            (oid, f"https://hampden.gov/maps?{seen}"),
        )
        store.db.execute(
            "INSERT INTO candidates (url, via, first_seen, endpoint_id) VALUES (?, 'crawl', ?, ?)",
            (url, seen, oid),
        )
    store.close()

    store = Store(path)
    ep = Endpoint(Platform.VIEWER, "https://www.axisgis.com/HampdenMA", vendor="AxisGIS")
    eps = store.query("SELECT id, url, first_seen FROM endpoints")
    assert [tuple(r) for r in eps] == [(ep.id, "https://next.axisgis.com/HampdenMA", "2024-01-01")]
    links = store.query("SELECT endpoint_id, jurisdiction_id FROM endpoint_jurisdictions")
    assert [tuple(r) for r in links] == [(ep.id, "osm:1")]
    cands = store.query("SELECT DISTINCT endpoint_id FROM candidates")
    assert [r[0] for r in cands] == [ep.id]
