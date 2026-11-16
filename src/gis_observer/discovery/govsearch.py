"""Read the GIS links govsearch has already found, straight from its OpenSearch index.

govsearch's spider stores `gis_links` (url + link text) on every page it indexes,
plus the OSM relation id of the town it was crawling. Reading those avoids
re-crawling sites govsearch already covers. Its index has no document ids, so
re-crawls duplicate pages; duplicate candidate URLs are dropped on insert.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from ..models import Candidate

QUERY = {
    "nested": {"path": "gis_links", "query": {"exists": {"field": "gis_links.url"}}},
}
SOURCE_FIELDS = ["url", "source_name", "osm_relation_id", "gis_links"]


async def read_gis_links(
    base_url: str = "http://localhost:9200",
    index: str = "govsites",
    *,
    page_size: int = 500,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[Candidate]:
    base = base_url.rstrip("/")
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=60)
    scroll_id = None
    try:
        body = {"size": page_size, "_source": SOURCE_FIELDS, "query": QUERY}
        resp = await client.post(f"{base}/{index}/_search", params={"scroll": "2m"}, json=body)
        resp.raise_for_status()
        data = resp.json()
        while True:
            scroll_id = data.get("_scroll_id", scroll_id)
            hits = (data.get("hits") or {}).get("hits") or []
            if not hits:
                break
            for hit in hits:
                for cand in _candidates(hit.get("_source") or {}):
                    yield cand
            resp = await client.post(
                f"{base}/_search/scroll", json={"scroll": "2m", "scroll_id": scroll_id}
            )
            resp.raise_for_status()
            data = resp.json()
    finally:
        if scroll_id:
            try:
                await client.request(
                    "DELETE", f"{base}/_search/scroll", json={"scroll_id": scroll_id}
                )
            except httpx.HTTPError:
                pass
        if own_client:
            await client.aclose()


def _candidates(doc: dict):
    osm = doc.get("osm_relation_id")
    jurisdiction_id = f"osm:{osm}" if osm else None
    for link in doc.get("gis_links") or []:
        url = (link.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            yield Candidate(
                url=url,
                via="govsearch",
                found_on=doc.get("url"),
                link_text=(link.get("text") or "").strip() or None,
                jurisdiction_id=jurisdiction_id,
            )
