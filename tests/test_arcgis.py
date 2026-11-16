import asyncio

from helpers import collect, make_fetcher

from gis_observer.harvesters import Harvester
from gis_observer.models import Asset, Candidate, Endpoint, Platform

ROOT = "https://gis.town.gov/arcgis/rest/services"

LAYERS = {
    "layers": [
        {
            "id": 0,
            "name": "Parcels",
            "type": "Feature Layer",
            "geometryType": "esriGeometryPolygon",
            "description": "Assessor parcels",
            "extent": {
                "xmin": -7926533.1, "ymin": 5200000.0, "xmax": -7900000.0, "ymax": 5231034.0,
                "spatialReference": {"wkid": 102100, "latestWkid": 3857},
            },
            "editingInfo": {"lastEditDate": 1716237128000},
            "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"}],
        },
        {"id": 1, "name": "Basemap", "type": "Group Layer", "geometryType": None},
    ],
    "tables": [{"id": 2, "name": "Owners", "type": "Table"}],
}  # fmt: skip

ROUTES = {
    ROOT: {
        "currentVersion": 11.5,
        "folders": ["Utilities", "Secure"],
        "services": [{"name": "Assessing", "type": "MapServer"}],
    },
    f"{ROOT}/Utilities": {
        "currentVersion": 11.5,
        "services": [
            {"name": "Utilities/Water", "type": "FeatureServer"},
            {"name": "Utilities/Geocoder", "type": "GeocodeServer"},
        ],
    },
    f"{ROOT}/Secure": {"error": {"code": 499, "message": "Token Required"}},
    f"{ROOT}/Assessing/MapServer": {
        "currentVersion": 11.5,
        "serviceDescription": "Assessing data",
        "copyrightText": "Town of Example",
        "spatialReference": {"wkid": 102100, "latestWkid": 3857},
        "fullExtent": {
            "xmin": 1,
            "ymin": 2,
            "xmax": 3,
            "ymax": 4,
            "spatialReference": {"wkid": 3857},
        },
        "layers": [{"id": 0, "name": "Parcels"}],
    },
    f"{ROOT}/Assessing/MapServer/layers": LAYERS,
    f"{ROOT}/Utilities/Water/FeatureServer": {"error": {"code": 499, "message": "Token Required"}},
    f"{ROOT}/Utilities/Geocoder/GeocodeServer": {"currentVersion": 11.5},
}


async def _harvest(routes):
    async with make_fetcher(routes) as http:
        h = Harvester.for_platform(Platform.ARCGIS_REST, http)
        items = await collect(h, Endpoint(Platform.ARCGIS_REST, ROOT))
        return h, items


def test_walks_folders_services_and_layers():
    h, items = asyncio.run(_harvest(ROUTES))
    assert h.errors == []
    by_key = {a.key: a for a in items if isinstance(a, Asset)}
    assert set(by_key) == {
        ROOT,
        f"{ROOT}/Assessing/MapServer",
        f"{ROOT}/Assessing/MapServer/0",
        f"{ROOT}/Assessing/MapServer/1",
        f"{ROOT}/Assessing/MapServer/2",
        f"{ROOT}/Utilities/Water/FeatureServer",
        f"{ROOT}/Utilities/Geocoder/GeocodeServer",
    }
    parcels = by_key[f"{ROOT}/Assessing/MapServer/0"]
    assert (parcels.kind, parcels.geometry_type, parcels.srid) == ("layer", "polygon", 3857)
    assert parcels.source_updated_at.year == 2024
    assert parcels.extra["fields"][0]["name"] == "OBJECTID"
    assert by_key[f"{ROOT}/Assessing/MapServer/1"].kind == "group"
    assert by_key[f"{ROOT}/Assessing/MapServer/2"].kind == "table"
    service = by_key[f"{ROOT}/Assessing/MapServer"]
    assert service.extra["copyrightText"] == "Town of Example"
    assert service.bbox == (1.0, 2.0, 3.0, 4.0)


def test_secured_service_is_recorded_not_an_error():
    h, items = asyncio.run(_harvest(ROUTES))
    water = next(a for a in items if a.key.endswith("Water/FeatureServer"))
    assert water.extra == {"access": "restricted", "error_code": 499}
    assert h.errors == []


def test_old_servers_without_layers_endpoint_fall_back():
    routes = dict(ROUTES)
    del routes[f"{ROOT}/Assessing/MapServer/layers"]
    h, items = asyncio.run(_harvest(routes))
    assert h.errors == []
    assert any(a.key == f"{ROOT}/Assessing/MapServer/0" for a in items)


def test_unreadable_root_is_an_error():
    h, items = asyncio.run(_harvest({ROOT: {"error": {"code": 403}}}))
    assert items == [] and h.errors


PORTAL = "https://town.maps.arcgis.com"


def test_portal_items_and_leads():
    routes = {
        f"{PORTAL}/sharing/rest/portals/self": {"id": "ORG1", "name": "Town GIS", "urlKey": "town"},
        f"{PORTAL}/sharing/rest/search": {
            "total": 3,
            "nextStart": -1,
            "results": [
                {
                    "id": "a" * 32, "type": "Feature Service", "title": "Parcels",
                    "url": "https://gisportal.town.gov/arcgis/rest/services/Parcels/MapServer/0",
                    "extent": [[-71.2, 42.2], [-70.9, 42.4]], "modified": 1716237128000,
                },
                {"id": "b" * 32, "type": "Web Mapping Application", "title": "Viewer"},
                {"id": "c" * 32, "type": "Web Map", "title": "Zoning map"},
            ],
        },
    }  # fmt: skip

    async def go():
        async with make_fetcher(routes) as http:
            h = Harvester.for_platform(Platform.ARCGIS_PORTAL, http)
            return h, await collect(h, Endpoint(Platform.ARCGIS_PORTAL, PORTAL))

    h, items = asyncio.run(go())
    assert h.errors == []
    assets = [i for i in items if isinstance(i, Asset)]
    assert [a.kind for a in assets] == ["portal", "service", "webmap"]
    assert assets[1].bbox == (-71.2, 42.2, -70.9, 42.4)
    leads = [i for i in items if isinstance(i, Candidate)]
    assert [c.url for c in leads] == [
        "https://gisportal.town.gov/arcgis/rest/services/Parcels/MapServer/0"
    ]
