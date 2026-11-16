"""Pull outbound links from an HTML page, including embedded map iframes."""

from __future__ import annotations

from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

SKIP_PREFIXES = ("#", "mailto:", "tel:", "javascript:", "data:", "sms:")


def extract_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """[(absolute url, link text)] in page order, without duplicates.

    Iframes matter here: towns often embed an ArcGIS or vendor map on a "Maps" page
    rather than linking to it.
    """
    soup = BeautifulSoup(html, "lxml")
    base_tag = soup.find("base", href=True)
    base = urljoin(page_url, base_tag["href"]) if base_tag else page_url
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(raw, text: str) -> None:
        raw = (raw or "").strip()
        if not raw or raw.lower().startswith(SKIP_PREFIXES):
            return
        try:
            url = urldefrag(urljoin(base, raw))[0]
        except ValueError:
            return
        if not url.startswith(("http://", "https://")) or url in seen:
            return
        seen.add(url)
        out.append((url, " ".join(text.split())[:300]))

    for tag in soup.find_all(["a", "area"], href=True):
        text = tag.get_text(" ", strip=True) or tag.get("title") or tag.get("aria-label") or ""
        add(tag["href"], text)
    for tag in soup.find_all(["iframe", "embed"]):
        add(tag.get("src") or tag.get("data-src"), tag.get("title") or "[embedded map]")
    return out
