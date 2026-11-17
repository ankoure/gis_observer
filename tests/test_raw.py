import asyncio

import httpx
from helpers import make_fetcher

from gis_observer.raw import RawCapture, read_raw

LATIN1_XML = "<?xml version='1.0' encoding='ISO-8859-1'?><Title>Chicopée</Title>".encode("latin-1")


def test_raw_capture_round_trips(tmp_path):
    routes = {
        "https://gis.town.gov/old": httpx.Response(
            301, headers={"location": "https://gis.town.gov/new"}
        ),
        "https://gis.town.gov/new": {"currentVersion": 11.1},
        "https://gis.town.gov/wms": httpx.Response(
            200, content=LATIN1_XML, headers={"content-type": "text/xml"}
        ),
        "https://gis.town.gov/parcels.zip": httpx.Response(
            200, headers={"etag": '"abc"', "content-length": "1234", "set-cookie": "s=1"}
        ),
    }

    async def go():
        async with make_fetcher(routes) as http:
            cap = RawCapture(http)
            await cap.get("https://gis.town.gov/old", params={"f": "json"})
            wms = await cap.get(
                "https://gis.town.gov/wms", params={"service": "WMS", "request": "GetCapabilities"}
            )
            await cap.head("https://gis.town.gov/parcels.zip")
            return cap, wms

    cap, wms = asyncio.run(go())
    path = cap.write(tmp_path / "raw.jsonl.gz")
    (rec, resp), (rec_wms, resp_wms), (rec_file, resp_file) = list(read_raw(path))

    # the request as made, not just where the redirect landed
    assert (rec["request_url"], rec["params"]) == ("https://gis.town.gov/old", {"f": "json"})
    assert resp.url == "https://gis.town.gov/new"
    assert resp.json() == {"currentVersion": 11.1}

    # non-UTF-8 bodies come back byte for byte
    assert "body_b64" in rec_wms
    assert resp_wms.content == wms.content == LATIN1_XML

    # headers the file harvester hashes survive; cookies aren't kept
    assert rec_file["method"] == "HEAD"
    assert resp_file.headers["etag"] == '"abc"'
    assert resp_file.headers["content-length"] == "1234"
    assert "set-cookie" not in resp_file.headers
