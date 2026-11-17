"""Link extraction, the site crawler, the prober and the govsearch reader."""

import asyncio

import httpx
from helpers import make_fetcher

from gis_observer.discovery.crawl import crawl_site
from gis_observer.discovery.govsearch import read_gis_links
from gis_observer.discovery.links import extract_links
from gis_observer.discovery.probe import probe
from gis_observer.models import Jurisdiction, Platform


def test_extract_links_includes_iframes():
    html = """<html><body>
      <a href="/gis">GIS &amp; Maps</a>
      <a href="mailto:x@town.gov">mail</a>
      <a href="#top">top</a>
      <a href="https://www.axisgis.com/TownMA/#map">Assessor map</a>
      <iframe title="Zoning" src="https://town.maps.arcgis.com/apps/instant/x/index.html?appid=1"></iframe>
    </body></html>"""
    links = extract_links(html, "https://www.town.gov/departments/")
    assert links == [
        ("https://www.town.gov/gis", "GIS & Maps"),
        ("https://www.axisgis.com/TownMA/", "Assessor map"),
        ("https://town.maps.arcgis.com/apps/instant/x/index.html?appid=1", "Zoning"),
    ]


SITE = {
    "https://www.town.gov/robots.txt": httpx.Response(
        200, text="User-agent: *\nDisallow: /private\n"
    ),
    "https://www.town.gov": '<a href="/departments">Departments</a> <a href="/gis-maps">GIS Maps</a>'
    '<a href="/private/gis">Private GIS</a> <a href="https://www.google.com/maps/place/x">Directions</a>',
    "https://www.town.gov/gis-maps": '<iframe src="https://www.axisgis.com/TownMA/"></iframe>'
    '<a href="https://gis.town.gov/arcgis/rest/services">Map services</a>'
    '<a href="https://county.gov/gis">County GIS</a> <a href="/files/parcels.kml">Parcels KML</a>',
    "https://www.town.gov/departments": '<a href="/departments/clerk">Clerk</a>',
    "https://www.town.gov/departments/clerk": "<p>nothing here</p>",
    "https://www.town.gov/private/gis": '<a href="https://secret.gov/arcgis/rest/services">x</a>',
}


def test_crawl_follows_gis_pages_and_obeys_robots():
    town = Jurisdiction("osm:relation/1", "Town", "https://www.town.gov")

    async def go():
        async with make_fetcher(SITE) as http:
            return await crawl_site(http, town, max_pages=10, max_depth=2)

    result = asyncio.run(go())
    assert (result.pages, result.failure) == (4, None)
    found = {c.url: c for c in result.candidates}
    assert set(found) == {
        "https://www.axisgis.com/TownMA/",
        "https://gis.town.gov/arcgis/rest/services",
        "https://county.gov/gis",  # off-site with GIS words: kept for the prober
        "https://www.town.gov/files/parcels.kml",
    }
    assert found["https://county.gov/gis"].found_on == "https://www.town.gov/gis-maps"
    assert all(c.jurisdiction_id == "osm:relation/1" for c in found.values())


def test_crawl_skips_document_links_and_weak_leftovers():
    """Granville's flood-map PDFs (CivicPlus DocumentCenter) shouldn't reach the prober."""
    links = "".join(
        f'<a href="/DocumentCenter/View/{i}/Flood-Map-{i}">Flood Map {i}</a>' for i in range(5)
    )
    site = {
        "https://www.town.gov": links
        + '<a href="/401/Street-Map">Street Map</a><a href="/gis">GIS Data</a>'
        + '<a href="/a">A</a><a href="/b">B</a>',
        "https://www.town.gov/a": "<p>a</p>",
        "https://www.town.gov/b": "<p>b</p>",
    }
    town = Jurisdiction("osm:relation/2", "Town", "https://www.town.gov")

    async def go():
        async with make_fetcher(site) as http:
            # A budget of 1 page: everything linked from the home page goes unvisited.
            return await crawl_site(http, town, max_pages=1)

    # Only the unvisited page with a strong GIS word is passed on to the prober.
    assert [c.url for c in asyncio.run(go()).candidates] == ["https://www.town.gov/gis"]


def test_probe_scans_a_maps_page():
    page = (
        "<a href='https://county.gov/arcgis/rest/services/Parcels/MapServer'>Parcels</a>"
        "<iframe src='https://www.arcgis.com/apps/dashboards/0123456789abcdef0123456789abcdef'></iframe>"
    )
    routes = {
        "https://county.gov/gis": page,
        "https://www.arcgis.com/sharing/rest/content/items/0123456789abcdef0123456789abcdef": {
            "orgId": "ORG1", "url": None,
        },
        "https://www.arcgis.com/sharing/rest/portals/ORG1": {"urlKey": "county", "customBaseUrl": "maps.arcgis.com"},
    }  # fmt: skip

    async def go():
        async with make_fetcher(routes) as http:
            return await probe(http, "https://county.gov/gis")

    result = asyncio.run(go())
    assert result.status == "found"
    assert {(e.platform, e.url) for e in result.endpoints} == {
        (Platform.ARCGIS_REST, "https://county.gov/arcgis/rest/services"),
        (Platform.ARCGIS_PORTAL, "https://county.maps.arcgis.com"),
    }


def test_probe_dead_link():
    async def go():
        async with make_fetcher({}) as http:
            return await probe(http, "https://town.gov/old-gis-page")

    assert asyncio.run(go()).status == "dead"


def test_govsearch_reader_scrolls():
    pages = [
        {
            "_scroll_id": "s1",
            "hits": {"hits": [{"_source": {
                "url": "https://www.longmeadowma.gov/maps", "osm_relation_id": "relation/1181603",
                "gis_links": [{"url": "https://www.axisgis.com/LongmeadowMA/", "text": "Assessor maps"}],
            }}]},
        },
        {"_scroll_id": "s1", "hits": {"hits": []}},
    ]  # fmt: skip
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(200, json=pages[len([c for c in calls if c[0] == "POST"]) - 1])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [c async for c in read_gis_links("http://os:9200", "govsites", client=client)]

    (cand,) = asyncio.run(go())
    assert cand.url == "https://www.axisgis.com/LongmeadowMA/"
    assert cand.jurisdiction_id == "osm:relation/1181603"
    assert cand.via == "govsearch"
    assert calls[-1] == ("DELETE", "/_search/scroll")


def test_crawl_records_why_nothing_was_fetched():
    town = Jurisdiction("osm:relation/3", "Town", "https://www.town.gov")
    challenge = httpx.Response(403, text="<title>Just a moment...</title>")

    async def go(routes):
        async with make_fetcher(routes) as http:
            return await crawl_site(http, town)

    blocked = asyncio.run(go({"https://www.town.gov": challenge}))
    assert (blocked.pages, blocked.failure) == (0, "blocked")
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    assert asyncio.run(go({"https://www.town.gov/robots.txt": robots})).failure == "robots"
    assert asyncio.run(go({})).failure == "http_404"

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    dead = asyncio.run(go({"https://www.town.gov": refuse}))
    assert (dead.pages, dead.failure) == (0, "dead")


def test_osm_seeds_take_state_and_kind(tmp_path):
    from gis_observer.discovery.seeds import load_osm_seeds

    path = tmp_path / "seeds.csv"
    path.write_text("@id,source_name,website\nrelation/1,Granville,townofgranville.net\n")
    (j,) = load_osm_seeds(path, state="ma", kind="Municipality")
    assert (j.id, j.state, j.kind, j.website) == (
        "osm:relation/1", "MA", "Municipality", "https://townofgranville.net",
    )  # fmt: skip
    assert load_osm_seeds(path)[0].state is None
