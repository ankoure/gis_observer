import asyncio

from helpers import make_fetcher

from gis_observer import pipeline
from gis_observer.classify import classify
from gis_observer.models import Asset, Candidate, Endpoint, Jurisdiction, Platform
from gis_observer.store import Store

TOWNS = [
    Jurisdiction("osm:1", "Chester", "https://townofchester.net", "Municipality", "MA"),
    Jurisdiction("osm:2", "Westfield", "https://www.cityofwestfield.org", "Municipality", "MA"),
    Jurisdiction("osm:3", "Wilbraham", "https://wilbraham-ma.gov", "Municipality", "MA"),
    Jurisdiction("osm:4", "Lee", "https://lee.ma.us", "Municipality", "MA"),
    Jurisdiction("osm:5", "Chester", "https://chesternh.org", "Municipality", "NH"),
]


def _store():
    store = Store(":memory:")
    store.upsert_jurisdictions(TOWNS)
    return store


def _links(store, ep=None):
    sql = "SELECT e.url, ej.jurisdiction_id, ej.relation FROM endpoint_jurisdictions ej "
    sql += "JOIN endpoints e ON e.id = ej.endpoint_id"
    rows = store.query(sql + (" WHERE e.id = ?" if ep else "") + " ORDER BY 1, 2",
                       (ep.id,) if ep else ())  # fmt: skip
    return [tuple(r) for r in rows]


def _lead(store, parent, url, found_on=None):
    pipeline.ingest_candidates(store, [Candidate(url, f"harvest:{parent.id}", found_on=found_on)])


def test_viewer_slugs_name_their_town():
    store = _store()
    hub = Endpoint(
        Platform.DCAT, "https://geo-massdot.opendata.arcgis.com/api/feed/dcat-us/1.1.json"
    )
    store.upsert_endpoint(hub)
    for url in (
        "https://www.axisgis.com/ChesterMA/",  # state suffix picks the MA Chester
        "https://gis.vgsi.com/westfieldma/",
        "https://westfieldma.mapgeo.io/",
        "https://www.axisgis.com/Chester/",  # two Chesters: ambiguous, not attributed
        "https://www.mapsonline.net/wilbraham/",  # bare name, but unique and a town viewer
    ):
        _lead(store, hub, url)
    got = {(url, jid, rel) for url, jid, rel in _links(store)}
    assert got == {
        ("https://www.axisgis.com/ChesterMA", "osm:1", "named"),
        ("https://gis.vgsi.com/westfieldma", "osm:2", "named"),
        ("https://westfieldma.mapgeo.io", "osm:2", "named"),
        ("https://www.mapsonline.net/wilbraham", "osm:3", "named"),
    }


def test_hosts_on_a_towns_domain_are_its_own():
    store = _store()
    portal = Endpoint(Platform.ARCGIS_PORTAL, "https://massgis.maps.arcgis.com")
    store.upsert_endpoint(portal)
    _lead(store, portal, "https://gis.wilbraham-ma.gov/arcgis/rest/services/Parcels/MapServer")
    _lead(store, portal, "https://maps.lee.ma.us/arcgis/rest/services")
    assert set(_links(store)) == {
        ("https://gis.wilbraham-ma.gov/arcgis/rest/services", "osm:3", "same_domain"),
        ("https://maps.lee.ma.us/arcgis/rest/services", "osm:4", "same_domain"),
    }


def test_shared_catalogs_pass_nothing_on():
    """Wilbraham linking MassGIS mustn't credit MassGIS's leads to Wilbraham."""
    store = _store()
    massgis = Endpoint(Platform.ARCGIS_PORTAL, "https://massgis.maps.arcgis.com")
    pipeline.ingest_candidates(
        store, [Candidate(massgis.url + "/home", "crawl", jurisdiction_id="osm:3")]
    )
    assert _links(store, massgis) == [(massgis.url, "osm:3", "linked")]
    _lead(
        store,
        massgis,
        "https://services1.arcgis.com/MassGISorg/arcgis/rest/services/X/FeatureServer",
    )
    assert _links(store) == [(massgis.url, "osm:3", "linked")]

    # Even a town that names it can't make a shared catalog its own.
    assert store.attribute(massgis) == []


def test_leads_inherit_from_a_towns_own_portal(tmp_path):
    store = _store()
    portal = Endpoint(Platform.ARCGIS_PORTAL, "https://westfieldma.maps.arcgis.com")
    pipeline.ingest_candidates(
        store, [Candidate(portal.url + "/apps/x", "crawl", jurisdiction_id="osm:2")]
    )
    # The urlKey names Westfield, which upgrades the crawl's `linked`.
    assert _links(store, portal) == [(portal.url, "osm:2", "named")]

    own = "https://services3.arcgis.com/WESTorg/arcgis/rest/services/Parcels/FeatureServer"
    other_org = "https://services1.arcgis.com/MassGISorg/arcgis/rest/services/L/FeatureServer"
    state = "https://gis.massdot.state.ma.us/arcgis/rest/services/Roads/MapServer"
    county = "https://gis.hampdencounty.gov/arcgis/rest/services/X/MapServer"
    vendor = "https://hostingdata3.tighebond.com/arcgis/rest/services/WestfieldMA/MapServer"
    items = [
        {"id": f"{i:032x}", "type": "Map Service", "title": f"item {i}", "url": url}
        for i, url in enumerate([own, other_org, state, county, vendor])
    ]
    routes = {
        f"{portal.url}/sharing/rest/portals/self": {
            "id": "WESTorg", "urlKey": "westfieldma", "name": "City of Westfield"
        },
        f"{portal.url}/sharing/rest/search": {"nextStart": -1, "results": items},
    }  # fmt: skip

    async def go():
        async with make_fetcher(routes) as http:
            return await pipeline.harvest_one(store, http, tmp_path, portal)

    assert asyncio.run(go()) == "ok"
    inherited = {url for url, jid, rel in _links(store) if rel == "via_endpoint"}
    # Not MassGIS's org, the state's or county's servers, or a consultant's shared server.
    assert inherited == {"https://services3.arcgis.com/WESTorg/arcgis/rest/services"}
    assert {jid for _, jid, rel in _links(store) if rel == "via_endpoint"} == {"osm:2"}
    evidence = store.query(
        "SELECT evidence_url FROM endpoint_jurisdictions WHERE relation = 'via_endpoint'"
    )
    assert all("home/item.html?id=" in r[0] for r in evidence)


def test_linked_parents_pass_nothing_on():
    store = _store()
    portal = Endpoint(Platform.ARCGIS_PORTAL, "https://somecompany.maps.arcgis.com")
    pipeline.ingest_candidates(
        store, [Candidate(portal.url + "/home", "crawl", jurisdiction_id="osm:1")]
    )
    _lead(store, portal, "https://services.arcgis.com/X/arcgis/rest/services/A/FeatureServer")
    assert _links(store) == [(portal.url, "osm:1", "linked")]


def test_portal_title_names_its_town():
    store = _store()
    portal = Endpoint(Platform.ARCGIS_PORTAL, "https://cogis.maps.arcgis.com")
    store.upsert_endpoint(portal)
    hid = store.start_harvest(portal.id)
    store.apply_assets(portal.id, hid, [Asset(portal.url, "portal", title="Town of Wilbraham GIS",
                                              extra={"org_id": "W"})], complete=True)  # fmt: skip
    assert store.attribute(portal) == ["osm:3"]
    assert _links(store, portal) == [(portal.url, "osm:3", "named")]


def test_attribute_all_backfills_old_catalogs():
    store = _store()
    portal = Endpoint(Platform.ARCGIS_PORTAL, "https://wilbrahamma.maps.arcgis.com")
    store.upsert_endpoint(portal)
    lead = "https://services9.arcgis.com/W/arcgis/rest/services/P/FeatureServer"
    viewer = "https://www.axisgis.com/ChesterMA/"
    # As an old run left them: endpoints and classified candidates, no links.
    for url in (lead, viewer):
        store.add_candidate(Candidate(url, f"harvest:{portal.id}", found_on=portal.url))
        ep = classify(url)
        store.upsert_endpoint(ep, via=f"harvest:{portal.id}")
        store.set_candidate_status(url, "classified", ep.id)
    store.commit()
    assert _links(store) == []

    changes = store.attribute_all()
    assert changes == {"same_domain": 0, "named": 2, "via_endpoint": 1, "linked": 0}
    assert set(_links(store)) == {
        (portal.url, "osm:3", "named"),
        ("https://www.axisgis.com/ChesterMA", "osm:1", "named"),
        ("https://services9.arcgis.com/W/arcgis/rest/services", "osm:3", "via_endpoint"),
    }
    assert store.attribute_all() == {"same_domain": 0, "named": 0, "via_endpoint": 0, "linked": 0}
