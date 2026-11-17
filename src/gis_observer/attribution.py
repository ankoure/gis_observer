"""Which jurisdiction an endpoint belongs to, judged from the endpoint itself.

Discovery links an endpoint to the jurisdiction whose site pointed at it. Endpoints
found inside other catalogs have no such jurisdiction, so they're attributed here:

- **By content.** An endpoint on a jurisdiction's own domain is `same_domain`. One
  whose URL or portal names the town (axisgis.com/ChesterMA, westfieldma.maps.arcgis.com,
  a portal titled "Town of Wilbraham") is `named`.
- **By parent.** A lead found in a catalog a jurisdiction *owns* (`same_domain` or
  `named`) is `via_endpoint`. A town linking to a catalog doesn't make it theirs, so
  `linked` parents pass nothing on, and neither do the shared catalogs below.

Shared catalogs (state agencies, regional planners, consultants) point at many towns,
so copying their links would credit everything to whichever town linked them first.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from urllib.parse import urlsplit

from .classify import PATH_SCOPED_VIEWERS, host_matches
from .models import Endpoint, Platform

# Catalogs that serve many jurisdictions. Never attributed to a town, never inherited
# from, never inherited onto. Matched as host suffixes.
SHARED_HOSTS = (
    "mass.gov", "state.ma.us",  # MassGIS, MassDOT and other state agencies
    "massgis.maps.arcgis.com", "massdot.maps.arcgis.com", "geo-massdot.opendata.arcgis.com",
    "mapc.maps.arcgis.com", "pvpc.maps.arcgis.com",  # regional planning agencies
    "tighebond.maps.arcgis.com",  # consultant hosting many towns' maps
    "server.arcgisonline.com", "services.arcgisonline.com",  # Esri basemaps, Living Atlas
)  # fmt: skip
# Servers with one folder per client town: hostingdata3.tighebond.com/.../PalmerMA.
SHARED_HOST_PATTERNS = (re.compile(r"^hostingdata\d*\.tighebond\.com$"),)

# Website hosts that many organisations share; being on one says nothing about ownership.
SHARED_WEBSITE_HOSTS = (
    "sites.google.com", "wixsite.com", "squarespace.com", "wordpress.com", "github.io",
    "facebook.com", *SHARED_HOSTS,
)  # fmt: skip

# Relations that say the endpoint is the jurisdiction's own, strongest first. Only
# these pass a jurisdiction on to the leads found in the endpoint's catalog.
RANK = {"same_domain": 4, "named": 3, "via_endpoint": 2, "linked": 1}
OWNED = ("same_domain", "named")

# Hosts that belong to some government; a lead on one belongs to that government, not
# to the town whose catalog mentioned it.
GOV_TLDS = ("gov", "us", "mil", "edu")
# Hosted ArcGIS servers carry their organisation id: services3.arcgis.com/<orgId>/...
HOSTED_ORG = re.compile(r"^(?:services|tiles|features)\d*\.arcgis\.com$", re.I)

PREFIXES = ("townof", "cityof", "town", "city", "countyof")
STATE_NAMES = dict(
    pair.split(":")
    for pair in (
        "AL:alabama AK:alaska AZ:arizona AR:arkansas CA:california CO:colorado "
        "CT:connecticut DE:delaware FL:florida GA:georgia HI:hawaii ID:idaho IL:illinois "
        "IN:indiana IA:iowa KS:kansas KY:kentucky LA:louisiana ME:maine MD:maryland "
        "MA:massachusetts MI:michigan MN:minnesota MS:mississippi MO:missouri MT:montana "
        "NE:nebraska NV:nevada NH:newhampshire NJ:newjersey NM:newmexico NY:newyork "
        "NC:northcarolina ND:northdakota OH:ohio OK:oklahoma OR:oregon PA:pennsylvania "
        "RI:rhodeisland SC:southcarolina SD:southdakota TN:tennessee TX:texas UT:utah "
        "VT:vermont VA:virginia WA:washington WV:westvirginia WI:wisconsin WY:wyoming "
        "DC:districtofcolumbia"
    ).split()
)
# Words a portal title adds to the org's name: "Westfield GIS", "Town of Ware Maps".
TITLE_SUFFIXES = ("gisportal", "gis", "maps", "mapping", "online", "opendata", "hub")


def squash(text: str | None) -> str:
    """Lowercase letters and digits only: "Manchester-by-the-Sea" -> "manchesterbythesea"."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def bare_host(url: str | None) -> str:
    host = (urlsplit(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_shared(url: str | None) -> bool:
    host = bare_host(url)
    return any(host_matches(host, h) for h in SHARED_HOSTS) or any(
        p.match(host) for p in SHARED_HOST_PATTERNS
    )


def hosted_org(url: str) -> str | None:
    """The ArcGIS Online org id in a hosted server's URL, if it is one."""
    parts = urlsplit(url)
    if not HOSTED_ORG.match(parts.hostname or ""):
        return None
    seg = parts.path.strip("/").split("/")[0]
    return seg or None


def slugs(ep: Endpoint) -> list[tuple[str, bool]]:
    """Parts of an endpoint's URL that name its owner: `(slug, bare name allowed)`.

    A bare name ("chester") is only trusted on vendors that host one viewer per
    municipality; elsewhere ("chester.maps.arcgis.com" could be anyone) the slug has
    to carry a state or a "town of" to count.
    """
    parts = urlsplit(ep.url)
    host = (parts.hostname or "").lower()
    first_label = host.split(".")[0]
    if ep.platform is Platform.VIEWER:
        if any(host_matches(host, h) for h in PATH_SCOPED_VIEWERS):
            seg = parts.path.strip("/").split("/")[0]
            return [(seg, True)] if seg else []
        if host_matches(host, "mapgeo.io") and host != "mapgeo.io":
            return [(first_label, True)]
        return []
    if ep.platform is Platform.ARCGIS_PORTAL and host.endswith(".maps.arcgis.com"):
        return [(first_label, False)]
    if ep.platform is Platform.DCAT and host.endswith((".hub.arcgis.com", ".opendata.arcgis.com")):
        return [(first_label, False)]
    return []


def title_slug(title: str | None) -> str:
    s = squash(title)
    changed = True
    while changed:
        changed = False
        for suffix in TITLE_SUFFIXES:
            if s.endswith(suffix) and len(s) > len(suffix):
                s, changed = s[: -len(suffix)], True
    return s


class Matcher:
    """Looks jurisdictions up by website domain and by the ways a URL can name them."""

    def __init__(self, rows: Iterable) -> None:
        sites: dict[str, set[str]] = defaultdict(set)
        self.forms: dict[str, set[str]] = defaultdict(set)  # needs no bare-name trust
        self.bare: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            jid, state = r["id"], (r["state"] or "").upper()
            site = bare_host(r["website"])
            if site and not any(host_matches(site, h) for h in SHARED_WEBSITE_HOSTS):
                sites[site].add(jid)
            name = squash(r["name"])
            for p in ("townof", "cityof", "countyof"):  # "City of Boston" in dotgov seeds
                if name.startswith(p):
                    name = name[len(p) :]
                    break
            if len(name) < 3:
                continue
            self.bare[name].add(jid)
            named = [name] + [p + name for p in PREFIXES]
            for n in named[1:]:
                self.forms[n].add(jid)
            if state:
                for st in (state.lower(), STATE_NAMES.get(state, "")):
                    if not st:
                        continue
                    for n in named:
                        self.forms[n + st].add(jid)
                    self.forms[st + name].add(jid)
        # A website shared by several jurisdictions can't say which one owns an endpoint.
        self.sites = {s: next(iter(j)) for s, j in sites.items() if len(j) == 1}

    def by_host(self, url: str) -> str | None:
        """The jurisdiction whose website domain the URL sits on."""
        host = bare_host(url)
        labels = host.split(".")
        for i in range(len(labels) - 1):
            if jid := self.sites.get(".".join(labels[i:])):
                return jid
        return None

    def by_slug(self, slug: str, bare_ok: bool) -> str | None:
        """The one jurisdiction a slug names, or None if it names none or several."""
        s = squash(slug)
        hits = self.forms.get(s) or (self.bare.get(s) if bare_ok else None)
        return next(iter(hits)) if hits and len(hits) == 1 else None


def may_inherit(target: Endpoint, owner_site: str | None, parent_org: str | None) -> bool:
    """Whether a lead found in a jurisdiction's own catalog can be credited to it.

    Not when the lead is a shared catalog, sits on some other government's domain
    (a town's web map using a state server), or is a hosted server belonging to a
    different ArcGIS Online org than the portal that listed it.
    """
    if is_shared(target.url):
        return False
    host = bare_host(target.url)
    site = bare_host(owner_site) if owner_site else ""
    on_own_site = bool(site) and host_matches(host, site)
    if host.rsplit(".", 1)[-1] in GOV_TLDS and not on_own_site:
        return False
    org = hosted_org(target.url)
    return not (parent_org and org and org != parent_org)
