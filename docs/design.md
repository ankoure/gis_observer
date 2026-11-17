# Design

## Goal

A catalog of the GIS data that governments publish, whatever software they publish it
with. It answers questions like "which Massachusetts towns publish parcels, where, and
in what format?" and "what changed on these servers this month?". It stores each
layer's name, geometry type, fields, extent and last-edit date, and how it changed
over time. It does not download the features themselves.

## How it works

```
 seeds ──> crawl ──┐
 govsearch ────────┼──> candidates ──classify (offline)──> endpoints ──> harvest ──> assets
 URL imports ──────┘         ▲                │ unrecognised                │       + versions
                             │                └──> probe (network) ─────────┤
                             └──── leads found inside catalogs ─────────────┘
```

1. **Seeds.** Jurisdictions to look at: OpenStreetMap boundary relations with
   websites (govsearch's `seeds.csv`) or CISA's `.gov` registry, plus endpoints that
   are already known (the Munger's 518 ArcGIS servers).
2. **Discovery** produces *candidates*: URLs that might lead to GIS data.
   - `crawl`: the jurisdiction's own site, GIS-looking links first, iframes included,
     robots.txt obeyed.
   - `govsearch`: the `gis_links` govsearch already stored in OpenSearch.
   - `seeds urls`: anything else, such as Athena results from `sql/athena/`.
3. **Classify** maps a URL to a platform and a canonical endpoint root, without
   touching the network (`classify.py`). For example,
   `.../rest/services/X/MapServer/3/query` becomes that server's `.../rest/services`.
4. **Probe** handles what the classifier can't place. It resolves ArcGIS Online item
   links to the owning organisation, follows redirects, recognises catalog APIs by
   their JSON, scans "Maps & GIS" pages for links and embedded maps, and tries the
   usual paths on `gis.*` and `data.*` subdomains.
5. **Harvest** reads each endpoint's catalog with its platform's harvester. It yields
   *assets*, plus new candidates for anything that catalog points to elsewhere.

## Platforms

| Platform | Recognised by | Harvested via | Assets |
|---|---|---|---|
| `arcgis_rest` | `/rest/services` in the path | root → folders → services → `/layers` | server, services, layers, tables (fields, geometry, extent, last edit) |
| `arcgis_portal` | `*.maps.arcgis.com`, `/portal/home`, item ids on arcgis.com (probe) | `portals/self` + item search | portal, service/file/web map items; item URLs become leads |
| `ogc` | `service=WMS/WFS`, `/geoserver/`, `/wms`, `/wfs`, `/ows` | WMS + WFS GetCapabilities | services, named layers / feature types (CRS, WGS84 extent) |
| `ckan` | `/api/3/action`, CKAN generator meta tag (probe) | `package_search` paging | geospatial datasets + their geo resources; service resources become leads |
| `dcat` | `data.json`, Hub sites, `/api/feed/dcat` | one JSON fetch | geospatial datasets + distributions (Socrata, Hub, many more) |
| `file` | `.kml/.kmz/.geojson/.gpkg/.shp.zip/.gdb.zip`, Google My Maps | HEAD (headers only) | one file: size, type, Last-Modified, ETag |
| `viewer` | AxisGIS, MapGeo, PeopleGIS, Schneider Beacon/qPublic | not harvested | none; recorded so we know who uses which vendor |

A new platform is one module in `harvesters/` subclassing `Harvester` with a
`platform` attribute (registration is automatic), plus a rule in `classify.py`.

## Data model (SQLite, `store.py`)

- `jurisdictions`: who we look at. `crawl_pages` and `crawl_failure` record the last
  crawl's outcome: pages fetched, and when there were none, why (`blocked` for a 403
  or a Cloudflare challenge, `dead`, `robots`, `http_<status>`, `not_html`, `error`).
- `candidates`: every URL found, where it was found, its status
  (`new` → `classified` | `found` | `none` | `dead`), and the endpoint it led to.
- `endpoints`: catalog roots, keyed by `sha1(platform|url)`, with how they were found.
- `endpoint_jurisdictions`: which jurisdictions point at or own which endpoints,
  one row per pair with the strongest `relation` found:
  - `same_domain`: the endpoint is on the jurisdiction's own domain, so it's probably theirs.
  - `named`: the endpoint's URL or portal names the jurisdiction and no other
    (`axisgis.com/ChesterMA`, `westfieldma.maps.arcgis.com`, a portal titled "Town of
    Wilbraham"). A bare name like `chester` only counts on vendors with one viewer per
    town, and only if exactly one jurisdiction has that name.
  - `via_endpoint`: found inside a catalog the jurisdiction owns (`same_domain` or
    `named`), such as a hosted feature server listed in the town's own ArcGIS Online org.
  - `linked`: the jurisdiction only links to it. Fifty towns linking to MassGIS
    doesn't make MassGIS theirs.

  Endpoints found inside other catalogs are attributed by `attribution.py`. Shared
  catalogs (state agencies, regional planners, consultants: `SHARED_HOSTS`) point at
  many towns. They're never attributed or inherited from, so Wilbraham linking to
  MassGIS doesn't credit it with MassGIS's 1,243 assets. A lead isn't inherited if
  it's on another government's domain (`.gov`, `.us`), belongs to a different ArcGIS
  Online org than the portal that listed it, or is already owned by another
  jurisdiction. `gis-observer attribute` re-runs all of this over an existing catalog.
- `harvests`: one row per run per endpoint: status, counts, errors, raw file path.
- `assets`: current state, keyed by `(endpoint_id, key)`, where `key` is usually the URL.
- `asset_versions`: a row only when an asset is `new`, `changed`, `removed` or
  `returned`, with a JSON snapshot.
- `checks`: up/down history (ported from the Munger's `server_check.py` /
  `health_check.py`).
- `current_assets` (view): live assets joined to their endpoint.
- `jurisdiction_assets` (view): `current_assets` for each jurisdiction that points at
  the endpoint, with the `relation`. For example:

  ```sql
  SELECT jurisdiction_name, relation, platform, endpoint_url, name, format
  FROM jurisdiction_assets
  WHERE state = 'MA' AND lower(name) LIKE '%parcel%';
  ```

`report` puts each jurisdiction in one coverage group: an endpoint of its own (any
relation but `linked`),
only links to endpoints elsewhere, crawl failed, nothing found, or not crawled yet.
Towns crawled before crawl outcomes were recorded count as "nothing found" until
`crawl --recrawl`.

**Change rules.** An asset changes when its normalised record's hash changes. That
includes edits to name, description, fields, extent, or the source's last-edit date.
Only a complete harvest (no errors) can mark assets removed: after a partial one, a
missing asset may only be behind a request that failed. A secured service is not an
error; it's recorded with `extra.access = "restricted"`.

**Why SQLite.** At this scale (hundreds of thousands of assets) the catalog is mostly
upserts and needs no server. All SQL is in `store.py`, so moving to PostGIS (where the
2024 tables lived, on Crunchy Bridge) means rewriting one module. DuckDB can attach
the file directly for analysis.

**Known issue: flapping behind load balancers.** Some servers have nodes out of sync.
MassGIS's GeoServer returned capabilities with `updateSequence` 27440 (801 WFS
feature types) and 27784 (804) on back-to-back requests. Two harvests a minute apart
then report layers as new, changed and removed. The OGC service asset records
`updateSequence` so this can be seen. If it becomes a nuisance, one option is to only
report a removal once an asset has been missing for two consecutive complete harvests.

## Raw landing zone

Each harvest writes every response it received to
`data/raw/<platform>/<endpoint id>/<UTC timestamp>.jsonl.gz`. That matches the old
Airflow DAGs' `s3://restservicejson/{date}/{type}/{server}/{service}.json`. Moving
this to S3 later means replacing `RawCapture.write`.

**Replay.** `gis-observer replay --out NEW.sqlite [--endpoint ID ...] [--since DATE]`
re-runs today's harvesters over the raw files without fetching anything, so a parser
fix can be applied to history. `ReplayFetcher` stands in for the network: it answers
each request from the raw file, matched on method, requested URL and params. A
request the file doesn't have (a fixed parser asking for something new) fails, so
that harvest is partial and can't mark assets removed.

- Replay writes a copy. `--out` gets a copy of `--db` (which is never changed) and
  must not exist yet. Check the copy, then swap it in. Rewriting history in place
  would mix old and new parses if a replay stopped halfway.
- Each endpoint with raw files starts over. Its harvests, assets and versions are
  dropped, then its files are replayed oldest first. Endpoints without raw files keep
  what they had. `--since` skips older files, so that endpoint's history starts at
  that date.
- Harvests keep their original dates. A harvest's start is the first response's
  `fetched_at`; its finish, and the `observed_at` of its changes, is the file's
  timestamp.

## Politeness

- One request at a time per host, then a pause of 1.5 × the last response time
  (minimum 0.25 s, maximum 10 s). The 1.5× rule is the Munger's
  `time.sleep(elapsed * 1.5)`. The cap stops a 30 s timeout turning into a 45 s wait.
- Retries on 408/429/5xx (twice) and timeouts (once), with exponential backoff and
  honouring `Retry-After`. These are govsearch's retry codes.
- The crawler obeys robots.txt (RFC 9309: a 4xx robots.txt means no rules, a 5xx
  means stay out). Harvesters call documented public catalog APIs and don't consult
  robots.txt.
- Certificate failures are retried without verification and flagged (`tls_insecure`).
  Many government servers have broken chains, and the data is public.
- The User-Agent is `gis-observer/0.1`; set `GIS_OBSERVER_USER_AGENT` to add contact
  details.

## What carried over, and what changed

From **ArcGISRest_Munger / Airflow harvesters**
- Kept: Common Crawl × CISA `.gov` discovery (`sql/athena/`), the root → folders →
  services walk, the service-observation fields, raw JSON landing, health checks, and
  the pacing rule.
- Changed:
  - It goes down to layers and tables, using one `/layers` call per service.
  - The `?f=sitemap` path is gone; it gave URLs without names or types.
  - Secured services are recorded instead of skipped.
  - SQL is parameterised; the DAGs built queries with f-strings.
  - Failures are recorded per request instead of `raise SystemExit`.
  - Fixed a bug where `getServicesSitemapDisabled` re-requested the root inside the
    folder loop.

From **govsearch**
- Kept: OSM seeds and their relation ids (the jurisdiction key in both projects), the
  GIS keyword list, robots.txt, retry codes, and gentle per-domain concurrency.
- Changed:
  - No page text or embeddings; it only collects links.
  - Keywords match whole words, so "sitemap" no longer counts as "map".
  - Iframes are included.
  - It links to other sites as well as the town's own.
- govsearch itself is unchanged. It remains the full-text search product, and its
  index is one input here.

## Next steps

- **Socrata Discovery API** (`api.us.socrata.com/api/catalog/v1?domains=`). It's
  richer than Socrata's `data.json`, with column names and view types.
- **OpenDataSoft** (`/api/explore/v2.1/catalog/datasets`), **OGC API - Features**
  (`/collections`), and **WMTS**.
- **Web map resolution.** Read a Web Map item's `/data` to list the layers an app
  actually shows.
- **Subdomain guessing** from the CISA list: try `gis.<domain>` and `maps.<domain>`
  for every `.gov`. It's cheap and high-yield.
- **Reprojection.** Store a WGS84 bbox for every asset; ArcGIS extents are currently
  in their native SRS.
- **Scheduling.** Run `harvest` and `check` periodically, and alert on endpoints that
  start failing or lose many layers.
- **Viewer vendors.** Some (e.g. MapGeo) may expose JSON layer lists worth harvesting.
