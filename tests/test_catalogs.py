"""OGC, CKAN, DCAT and file harvesters."""

import asyncio

import httpx
from helpers import collect, make_fetcher

from gis_observer.classify import classify
from gis_observer.harvesters import Harvester
from gis_observer.models import Asset, Candidate, Endpoint, Platform

OWS = "https://gis-prod.digital.mass.gov/geoserver/ows"

# Trimmed from MassGIS's real capabilities documents.
WMS = """<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Service><Name>WMS</Name><Title>Massachusetts Data from MassGIS (GeoServer)</Title>
    <Abstract>Statewide Massachusetts data served by MassGIS via GeoServer.</Abstract></Service>
  <Capability><Layer><Title>root</Title>
    <Layer queryable="1">
      <Name>massgis_dep_21e_mcp</Name><Title>massgis_dep_21e_mcp</Title><Abstract/>
      <CRS>EPSG:26986</CRS>
      <EX_GeographicBoundingBox>
        <westBoundLongitude>-73.53</westBoundLongitude><eastBoundLongitude>-69.83</eastBoundLongitude>
        <southBoundLatitude>41.17</southBoundLatitude><northBoundLatitude>42.88</northBoundLatitude>
      </EX_GeographicBoundingBox>
    </Layer>
  </Layer></Capability>
</WMS_Capabilities>"""

WFS = """<?xml version="1.0" encoding="UTF-8"?>
<wfs:WFS_Capabilities version="2.0.0" xmlns="http://www.opengis.net/wfs/2.0"
    xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:ows="http://www.opengis.net/ows/1.1">
  <ows:ServiceIdentification><ows:Title>MassGIS WFS</ows:Title></ows:ServiceIdentification>
  <FeatureTypeList>
    <FeatureType xmlns:massgis="http://massgis.state.ma.us/featuretype">
      <Name>massgis:GISDATA.MINLL1_ARC</Name><Title>1 Minute Lat Long Grid</Title>
      <Abstract>Generated from pg_gisdata</Abstract>
      <DefaultCRS>urn:ogc:def:crs:EPSG::26986</DefaultCRS>
      <ows:WGS84BoundingBox><ows:LowerCorner>-74.121 40.46</ows:LowerCorner>
        <ows:UpperCorner>-68.355 43.538</ows:UpperCorner></ows:WGS84BoundingBox>
    </FeatureType>
  </FeatureTypeList>
</wfs:WFS_Capabilities>"""


def _run(platform, url, routes):
    async def go():
        async with make_fetcher(routes) as http:
            h = Harvester.for_platform(platform, http)
            return h, await collect(h, Endpoint(platform, url))

    return asyncio.run(go())


def test_ogc_wms_and_wfs():
    def caps(request: httpx.Request):
        return WMS if request.url.params["service"] == "WMS" else WFS

    h, items = _run(Platform.OGC, OWS, {OWS: caps})
    assert h.errors == []
    layers = {a.key: a for a in items if a.kind == "layer"}
    wms = layers[f"{OWS}#WMS:massgis_dep_21e_mcp"]
    assert (wms.srid, wms.bbox, wms.bbox_srid) == (26986, (-73.53, 41.17, -69.83, 42.88), 4326)
    wfs = layers[f"{OWS}#WFS:massgis:GISDATA.MINLL1_ARC"]
    assert (wfs.title, wfs.srid, wfs.bbox) == (
        "1 Minute Lat Long Grid",
        26986,
        (-74.121, 40.46, -68.355, 43.538),
    )
    assert {a.title for a in items if a.kind == "service"} == {
        "Massachusetts Data from MassGIS (GeoServer)",
        "MassGIS WFS",
    }


def test_ogc_wms_only_server_is_fine():
    exception = '<?xml version="1.0"?><ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows"/>'

    def caps(request):
        return WMS if request.url.params["service"] == "WMS" else exception

    h, items = _run(Platform.OGC, OWS, {OWS: caps})
    assert h.errors == [] and len(items) == 2


CKAN = "https://data.town.gov"


def test_ckan_keeps_geo_datasets_and_follows_services():
    def page(request):
        start = int(request.url.params["start"])
        results = [
            {
                "id": "1",
                "name": "parcels",
                "title": "Parcels",
                "notes": "Assessor parcels",
                "metadata_modified": "2024-05-01T12:00:00.123456",
                "organization": {"title": "Assessing"},
                "extras": [
                    {
                        "key": "spatial",
                        "value": '{"type":"Polygon","coordinates":[[[-71.2,42.2],[-70.9,42.2],[-70.9,42.4],[-71.2,42.2]]]}',
                    }
                ],
                "resources": [
                    {
                        "id": "r1",
                        "format": "GeoJSON",
                        "url": f"{CKAN}/parcels.geojson",
                        "name": "GeoJSON",
                    },
                    {
                        "id": "r2",
                        "format": "Esri REST",
                        "name": "Service",
                        "url": "https://gis.town.gov/arcgis/rest/services/Parcels/MapServer/0",
                    },
                    {"id": "r3", "format": "PDF", "url": f"{CKAN}/parcels.pdf"},
                ],
            },
            {
                "id": "2",
                "name": "budget",
                "title": "Budget",
                "resources": [{"id": "r4", "format": "CSV", "url": f"{CKAN}/budget.csv"}],
            },
        ][start : start + 1]  # one per page, to exercise paging
        return {"success": True, "result": {"count": 2, "results": results}}  # fmt: skip

    h, items = _run(Platform.CKAN, CKAN, {f"{CKAN}/api/3/action/package_search": page})
    assert h.errors == []
    assets = [i for i in items if isinstance(i, Asset)]
    assert [(a.kind, a.format) for a in assets] == [
        ("dataset", None),
        ("file", "GeoJSON"),
        ("service", "Esri REST"),
    ]
    assert assets[0].bbox == (-71.2, 42.2, -70.9, 42.4)
    assert assets[0].extra["organization"] == "Assessing"
    assert [c.url for c in items if isinstance(c, Candidate)] == [
        "https://gis.town.gov/arcgis/rest/services/Parcels/MapServer/0"
    ]


FEED = "https://town-hub.opendata.arcgis.com/api/feed/dcat-us/1.1.json"


def test_dcat_hub_feed():
    feed = {
        "conformsTo": "https://project-open-data.cio.gov/v1.1/schema",
        "dataset": [
            {
                "identifier": "https://town-hub.opendata.arcgis.com/datasets/streets",
                "title": "Street Segments",
                "modified": "2024-05-13T13:40:53.000Z",
                "spatial": "-71.1850,42.2312,-70.9245,42.3951",
                "distribution": [
                    {"format": "Web Page", "mediaType": "text/html", "accessURL": "https://town-hub.opendata.arcgis.com/datasets/streets"},
                    {"format": "ArcGIS GeoServices REST API", "mediaType": "application/json",
                     "accessURL": "https://gisportal.town.gov/arcgis/rest/services/SAM/FeatureServer/3"},
                    {"format": "ZIP", "mediaType": "application/zip",
                     "downloadURL": "https://town-hub.opendata.arcgis.com/api/download/v1/items/abc/shapefile?layers=3"},
                ],
            },
            {"identifier": "budget-2024", "title": "Budget", "spatial": "Example, MA",
             "distribution": [{"format": "CSV", "downloadURL": "https://x/budget.csv"}]},
        ],
    }  # fmt: skip
    h, items = _run(Platform.DCAT, FEED, {FEED: feed})
    assert h.errors == []
    assets = [i for i in items if isinstance(i, Asset)]
    assert [(a.kind, a.format) for a in assets] == [
        ("dataset", None), ("service", "Esri REST"), ("file", "Shapefile"),
    ]  # fmt: skip
    assert assets[0].bbox == (-71.185, 42.2312, -70.9245, 42.3951)
    assert assets[0].source_updated_at.isoformat() == "2024-05-13T13:40:53+00:00"
    assert len([i for i in items if isinstance(i, Candidate)]) == 1


def test_broken_hub_feed_leads_to_the_owning_org():
    # East Longmeadow's real failure: the site is up but its catalog isn't configured.
    broken = httpx.Response(400, json={"error": "Site catalog is not configured correctly."})
    routes = {
        FEED: broken,
        "https://hub.arcgis.com/api/v3/domains/town-hub.opendata.arcgis.com": {
            "siteId": "0c6199d2cbbf4acfaff09050181cb25b", "orgId": "ORG1", "orgKey": "Town",
        },
        "https://www.arcgis.com/sharing/rest/portals/ORG1": {
            "id": "ORG1", "urlKey": "Town", "customBaseUrl": "maps.arcgis.com",
        },
    }  # fmt: skip
    h, items = _run(Platform.DCAT, FEED, routes)
    assert h.errors == [f"{FEED}: HTTP 400"]  # still a failed harvest
    (lead,) = items
    assert isinstance(lead, Candidate)
    assert (lead.url, lead.found_on) == ("https://town.maps.arcgis.com", FEED)
    assert classify(lead.url).platform is Platform.ARCGIS_PORTAL


def test_broken_non_hub_feed_has_no_fallback():
    url = "https://data.town.gov/data.json"
    h, items = _run(Platform.DCAT, url, {})
    assert (h.errors, items) == ([f"{url}: HTTP 404"], [])


def test_file_headers_only():
    url = "https://gis.town.gov/download/parcels.gdb.zip"
    head = httpx.Response(
        200,
        headers={
            "content-type": "application/zip",
            "content-length": "123456",
            "last-modified": "Wed, 01 May 2024 12:00:00 GMT",
            "etag": '"abc"',
        },
    )
    h, items = _run(Platform.FILE, url, {url: head})
    (asset,) = items
    assert (asset.name, asset.format) == ("parcels.gdb.zip", "File Geodatabase")
    assert asset.extra["content_length"] == 123456
    assert asset.source_updated_at.year == 2024
