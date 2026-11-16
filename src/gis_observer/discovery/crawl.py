"""Crawl a jurisdiction's own website for links to GIS data.

This is govsearch's spider cut down to what gis_observer needs: no page text, no
embeddings, no OpenSearch. It keeps the seeds format, the GIS keyword list and the
politeness rules (robots.txt, one request per host at a time), follows GIS-looking
links first, and records links to GIS platforms on any host. Most small towns don't
run their own map server; they link out to a vendor viewer, ArcGIS Online or MassGIS.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from ..classify import classify, has_gis_keyword, is_gis_candidate
from ..http import HttpGetter
from ..models import Candidate, Jurisdiction
from .links import extract_links

log = logging.getLogger(__name__)

ROBOTS_AGENT = "gis-observer"
SKIP_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".jpg", ".jpeg", ".png",
    ".gif", ".svg", ".zip", ".mp3", ".mp4", ".mov", ".ics", ".kml", ".kmz", ".csv",
    ".txt", ".xml", ".json", ".geojson",
)  # fmt: skip
# Document stores on CivicPlus sites (a large share of US town websites). Their URLs
# have no extension but always serve a file, typically a PDF.
SKIP_PATH_PARTS = ("/documentcenter/view/", "/archivecenter/viewfile/", "/agendacenter/viewfile/")


async def load_robots(http: HttpGetter, site_url: str) -> RobotFileParser:
    """RFC 9309: a 4xx robots.txt means no rules; a 5xx means stay out."""
    parts = urlsplit(site_url)
    robots = RobotFileParser()
    resp = await http.get(f"{parts.scheme}://{parts.netloc}/robots.txt", max_bytes=500_000)
    if resp.status is not None and resp.status >= 500:
        robots.disallow_all = True
    elif resp.ok:
        robots.parse(resp.text.splitlines())
    else:
        robots.allow_all = True
    return robots


def _bare(host: str | None) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _followable(url: str, text: str = "") -> bool:
    path = urlsplit(url).path.lower()
    if path.endswith(SKIP_EXTENSIONS) or any(part in path for part in SKIP_PATH_PARTS):
        return False
    return "(pdf)" not in text.lower()


async def crawl_site(
    http: HttpGetter, jurisdiction: Jurisdiction, *, max_pages: int = 40, max_depth: int = 2
) -> list[Candidate]:
    if not jurisdiction.website:
        return []
    start = jurisdiction.website
    robots = await load_robots(http, start)
    order = itertools.count()
    queue: list[tuple[int, int, int, str]] = [(0, 0, next(order), start)]
    queued = {start}
    visited: set[str] = set()
    site_hosts = {_bare(urlsplit(start).hostname)}
    found: dict[str, Candidate] = {}
    gis_pages: dict[str, Candidate] = {}  # on-site pages that look like a "Maps & GIS" page

    while queue and len(visited) < max_pages:
        _, depth, _, url = heapq.heappop(queue)
        if not robots.can_fetch(ROBOTS_AGENT, url):
            continue
        resp = await http.get(url, max_bytes=5_000_000)
        if not resp.ok or "html" not in resp.headers.get("content-type", "").lower():
            continue
        visited.add(url)
        site_hosts.add(_bare(urlsplit(resp.url).hostname))  # town.gov -> www.town.gov redirects

        for link, text in extract_links(resp.text, resp.url):
            on_site = _bare(urlsplit(link).hostname) in site_hosts
            gis_word = has_gis_keyword(link, text)
            if link not in found and (
                classify(link, text) is not None
                or (gis_word and not on_site and is_gis_candidate(link, text))
            ):
                found[link] = Candidate(
                    url=link,
                    via="crawl",
                    found_on=resp.url,
                    link_text=text or None,
                    jurisdiction_id=jurisdiction.id,
                )
            if not on_site or link in queued or not _followable(link, text):
                continue
            if not robots.can_fetch(ROBOTS_AGENT, link):
                continue  # don't hand disallowed pages to the prober either
            if has_gis_keyword(link, text, strong=True):
                gis_pages.setdefault(
                    link, Candidate(link, "crawl", resp.url, text or None, jurisdiction.id)
                )
            # GIS-looking pages go first and may go one level deeper than the rest.
            if depth + 1 <= (max_depth + 1 if gis_word else max_depth):
                queued.add(link)
                heapq.heappush(queue, (0 if gis_word else 1, depth + 1, next(order), link))

    # GIS pages the page budget didn't reach still get a look from the prober.
    for url, cand in gis_pages.items():
        if url not in visited and url not in found:
            found[url] = cand
    log.info("%s: %d pages, %d candidates", jurisdiction.name, len(visited), len(found))
    return list(found.values())
