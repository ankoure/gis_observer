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
    assert _apply(store, [a2]) == {"new": 0, "changed": 1, "removed": 1, "returned": 0}
    assert _apply(store, [a2, b]) == {"new": 0, "changed": 0, "removed": 0, "returned": 1}

    history = store.query("SELECT key, change FROM asset_versions ORDER BY harvest_id, key")
    assert [tuple(r) for r in history] == [
        ("a", "new"), ("b", "new"), ("a", "changed"), ("b", "removed"), ("b", "returned"),
    ]  # fmt: skip
    live = store.query("SELECT key FROM current_assets ORDER BY key")
    assert [r["key"] for r in live] == ["a", "b"]


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
