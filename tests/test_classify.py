import pytest

from gis_observer.classify import canonical, classify, has_gis_keyword, is_gis_candidate
from gis_observer.formats import geo_format
from gis_observer.models import Platform

CASES = [
    # ArcGIS Server: any URL under the services directory resolves to its root
    (
        "https://gis.cambridgema.gov/arcgis/rest/services/CDDBasemap/MapServer/3/query?where=1",
        Platform.ARCGIS_REST,
        "https://gis.cambridgema.gov/arcgis/rest/services",
    ),
    (
        "https://services3.arcgis.com/AbC123/arcgis/rest/services/Parcels/FeatureServer",
        Platform.ARCGIS_REST,
        "https://services3.arcgis.com/AbC123/arcgis/rest/services",
    ),
    (
        "http://maps.co.palm-beach.fl.us:80/arcgis/rest/services/",
        Platform.ARCGIS_REST,
        "http://maps.co.palm-beach.fl.us/arcgis/rest/services",
    ),
    # ArcGIS Online org and Enterprise portals
    (
        "https://boston.maps.arcgis.com/apps/webappviewer/index.html?id=abc",
        Platform.ARCGIS_PORTAL,
        "https://boston.maps.arcgis.com",
    ),
    (
        "https://gis.town.gov/portal/apps/dashboards/0123456789abcdef0123456789abcdef",
        Platform.ARCGIS_PORTAL,
        "https://gis.town.gov/portal",
    ),
    # ArcGIS Hub sites -> their DCAT feed
    (
        "https://bostonopendata-boston.opendata.arcgis.com/datasets/boston::parcels",
        Platform.DCAT,
        "https://bostonopendata-boston.opendata.arcgis.com/api/feed/dcat-us/1.1.json",
    ),
    ("https://data.ny.gov/data.json", Platform.DCAT, "https://data.ny.gov/data.json"),
    # OGC
    (
        "https://gis-prod.digital.mass.gov/geoserver/wms?service=WMS&request=GetCapabilities",
        Platform.OGC,
        "https://gis-prod.digital.mass.gov/geoserver/ows",
    ),
    (
        "https://maps.example.gov/cgi-bin/mapserv?map=/maps/town.map&SERVICE=WFS&REQUEST=GetCapabilities",
        Platform.OGC,
        "https://maps.example.gov/cgi-bin/mapserv?map=%2Fmaps%2Ftown.map",
    ),
    # CKAN
    (
        "https://data.boston.gov/api/3/action/package_show?id=traffic-signals",
        Platform.CKAN,
        "https://data.boston.gov",
    ),
    # Vendor viewers
    ("https://www.axisgis.com/AmherstMA/", Platform.VIEWER, "https://www.axisgis.com/AmherstMA"),
    (
        "https://amherstma.mapgeo.io/datasets/properties",
        Platform.VIEWER,
        "https://amherstma.mapgeo.io",
    ),
    (
        "https://gis.vgsi.com/longmeadowma/Search.aspx",
        Platform.VIEWER,
        "https://gis.vgsi.com/longmeadowma",
    ),
    # Tighe & Bond: one viewer per first path segment
    (
        "http://hosting.tighebond.com/palmerma_Public/",
        Platform.VIEWER,
        "http://hosting.tighebond.com/palmerma_Public",
    ),
    (
        "http://hosting.tighebond.com/chicopeema_public/",
        Platform.VIEWER,
        "http://hosting.tighebond.com/chicopeema_public",
    ),
    (
        "http://hosting.tighebond.com/AgawamMA_public/",
        Platform.VIEWER,
        "http://hosting.tighebond.com/AgawamMA_public",
    ),
    (
        "http://hosting.tighebond.com/wilbraham/main.htm",
        Platform.VIEWER,
        "http://hosting.tighebond.com/wilbraham",
    ),
    # Files
    (
        "https://gis.cambridgema.gov/download/gdb/TRAFFIC_Signals.gdb.zip",
        Platform.FILE,
        "https://gis.cambridgema.gov/download/gdb/TRAFFIC_Signals.gdb.zip",
    ),
    (
        "https://www.google.com/maps/d/viewer?mid=1AbCdEf&ll=42,-71",
        Platform.FILE,
        "https://www.google.com/maps/d/kml?mid=1AbCdEf&forcekml=1",
    ),
]


@pytest.mark.parametrize(("url", "platform", "endpoint_url"), CASES)
def test_classify(url, platform, endpoint_url):
    ep = classify(url)
    assert ep is not None, url
    assert (ep.platform, ep.url) == (platform, endpoint_url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/maps/place/Town+Hall",  # directions, not data
        "https://www.arcgis.com/apps/dashboards/0123456789abcdef0123456789abcdef",  # needs probe
        "https://www.town.gov/departments/planning",
        "mailto:gis@town.gov",
    ],
)
def test_classify_none(url):
    assert classify(url) is None


def test_zip_needs_a_gis_hint():
    assert classify("https://town.gov/files/budget.zip") is None
    ep = classify("https://town.gov/files/data.zip", "Parcels shapefile")
    assert ep is not None and ep.platform is Platform.FILE


def test_keywords_are_words():
    assert has_gis_keyword("https://town.gov/gis")
    assert has_gis_keyword("https://town.gov/x", "Interactive Maps")
    assert has_gis_keyword("https://town.gov/x", "Open Data Portal")
    assert not has_gis_keyword("https://town.gov/sitemap")
    assert not is_gis_candidate("https://maps.google.com/?q=town+hall", "Map")


@pytest.mark.parametrize(
    "url, text",
    [
        ("https://maps.live.com/default.aspx?where1=1+Main+St", "Bing"),
        ("http://maps.yahoo.com/maps_result?addr=1+Main+St", "Directions"),
        ("https://view.officeapps.live.com/op/embed.aspx?src=x", "Zoning map"),
    ],
)
def test_consumer_map_sites_are_not_candidates(url, text):
    assert not is_gis_candidate(url, text)


def test_arcgis_endpoint_identity_ignores_case_except_on_arcgis_com():
    a = classify("https://gisportal.boston.gov/arcgis/rest/services/X/MapServer")
    b = classify("https://gisportal.boston.gov/ArcGIS/rest/services/Y/MapServer")
    assert a.id == b.id and a.url != b.url
    hosted = classify(
        "https://services.arcgis.com/sFnw0xNflSi8J0uh/arcgis/rest/services/X/FeatureServer"
    )
    other = classify(
        "https://services.arcgis.com/sfnw0xnflsi8j0uh/arcgis/rest/services/X/FeatureServer"
    )
    assert hosted.id != other.id


def test_canonical():
    assert canonical("HTTPS://GIS.Town.GOV:443//arcgis/rest/services/#x") == (
        "https://gis.town.gov/arcgis/rest/services"
    )


@pytest.mark.parametrize(
    ("fmt", "url", "media", "expected"),
    [
        ("ArcGIS GeoServices REST API", None, None, "Esri REST"),
        (
            "ZIP",
            "https://h.arcgis.com/api/download/v1/items/abc/shapefile?layers=3",
            None,
            "Shapefile",
        ),
        (None, None, "application/vnd.geo+json", "GeoJSON"),
        ("CSV", "https://data.town.gov/x.csv", "text/csv", None),
        (
            None,
            "https://x.gov/arcgis/rest/services/A/MapServer/0/query?f=geojson",
            None,
            "Esri REST",
        ),
    ],
)
def test_geo_format(fmt, url, media, expected):
    assert geo_format(fmt, url, media) == expected


@pytest.mark.parametrize(
    "urls",
    [
        ["http://gis.vgsi.com/longmeadowma", "https://gis.vgsi.com/LongmeadowMA/Search.aspx"],
        ["https://www.axisgis.com/hampdenma", "https://next.axisgis.com/HampdenMA/"],
        ["https://www.axisgis.com/SouthwickMA", "https://next.axisgis.com/SouthwickMA"],
        ["http://amherstma.mapgeo.io", "https://AmherstMA.mapgeo.io/datasets/properties"],
    ],
)
def test_one_towns_viewer_is_one_endpoint(urls):
    assert len({classify(u).id for u in urls}) == 1


def test_viewers_of_different_towns_stay_apart():
    a = classify("https://www.axisgis.com/HampdenMA")
    b = classify("https://www.axisgis.com/SouthwickMA")
    c = classify("https://southwickma.mapgeo.io")
    assert len({a.id, b.id, c.id}) == 3
