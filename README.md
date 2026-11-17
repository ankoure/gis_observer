# gis_observer

Finds public GIS data published by governments, catalogs it down to the layer, and
records how it changes over time. It covers ArcGIS servers and every other platform
governments use: ArcGIS Online, GeoServer and other WMS/WFS servers, CKAN, Socrata and
other DCAT portals, plain file downloads, and vendor map viewers.

It combines two earlier projects:

- **ArcGISRest_Munger** (2024) and its Airflow harvesters found ArcGIS servers on
  `.gov` domains via Common Crawl and walked their services. That walk is now
  `harvesters/arcgis_rest.py`, and it goes down to layers and tables.
- **govsearch** (2025) crawled town websites and noted links that looked like GIS.
  Its seeds, keyword list and politeness rules drive `discovery/crawl.py`, and
  `discovery/govsearch.py` reads the links it has already indexed.

See [docs/design.md](docs/design.md) for how the pieces fit and what each table means.

## Quick start

```sh
uv sync

# Who to look at, and servers already known
uv run gis-observer seeds osm data/seeds/ma_municipalities.csv --state MA --kind Municipality  # 349 MA towns (govsearch)
uv run gis-observer seeds arcgis-servers data/seeds/arcgis_servers_2024.csv  # 518 servers (Munger)

# Find GIS links on town websites, and identify the ones the classifier can't
uv run gis-observer crawl --limit 20
uv run gis-observer probe

# Catalog everything found, then summarise
uv run gis-observer harvest --limit 50
uv run gis-observer report
```

Endpoints harvested within `--older-than-days` (default 7) are skipped, and each run
records new, changed, removed and returned assets. Harvests also produce leads: a CKAN
dataset that points at an ArcGIS server, or an ArcGIS Online item hosted on a county
server. Those become endpoints, so running `probe` and `harvest` again widens the
catalog. Among endpoints never harvested, those a jurisdiction links to go first;
`harvest --linked-only` takes only those.

`cycle` runs that loop once: probe new candidates, harvest what's due, probe the
leads, harvest the endpoints found along the way, then `check`, and prints a summary.
Each stage takes a limit:

```sh
uv run gis-observer cycle --probe-limit 500 --harvest-limit 300 --lead-harvest-limit 200
```

Run it weekly on a machine that keeps `data/`; [deploy/README.md](deploy/README.md) has
a systemd timer and a cron line. Set `GIS_OBSERVER_USER_AGENT` there to something with a
contact URL.

To see what changed, run `changes`. It lists each endpoint's new, changed, removed and
returned assets, and the fields that differ for each changed one. An endpoint's first
harvest shows as one line. Below that it lists endpoints whose latest check failed, or
that failed and came back up:

```sh
uv run gis-observer changes --since 30d            # optional: --endpoint ID, --platform P, --kind layer
```

Other sources:

```sh
uv run gis-observer govsearch --url http://localhost:9200 --index govsites   # govsearch's gis_links
uv run gis-observer seeds dotgov current-full.csv --types City County --states MA RI
uv run gis-observer seeds urls athena_results.csv                            # any list of URLs
uv run gis-observer check                                                    # are endpoints up?
```

Every response a harvest receives is kept under `data/raw/`. After a parser fix,
rebuild history from those files into a new catalog without fetching anything:

```sh
uv run gis-observer replay --out data/replayed.sqlite            # optional: --endpoint ID, --since 2026-11-16
```

## Querying the catalog

Everything is in `data/observer.sqlite`. The `current_assets` view is the live catalog:

```sh
# python -m sqlite3 is built in (3.12+); the sqlite3 CLI or DuckDB work too
uv run python -m sqlite3 data/observer.sqlite "
  SELECT platform, endpoint_url, name, geometry_type, source_updated_at
  FROM current_assets
  WHERE kind = 'layer' AND lower(name) LIKE '%parcel%'
  ORDER BY source_updated_at DESC LIMIT 20"
```

The raw responses behind every harvest are in `data/raw/<platform>/<endpoint>/<time>.jsonl.gz`.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```

## Layout

```
src/gis_observer/
  classify.py        URL -> platform + endpoint root, offline
  formats.py         is this resource geospatial, and which format
  http.py            polite async client (per-host pacing, retries, TLS fallback)
  raw.py             landing zone for raw responses
  store.py           SQLite schema, upserts, change tracking
  pipeline.py        crawl / probe / harvest / check / cycle, wired to the store
  cli.py
  discovery/         seeds, site crawler, govsearch reader, prober, link extraction
  harvesters/        arcgis_rest, arcgis_portal, ogc, ckan, dcat, files
data/seeds/          MA municipalities (govsearch), ArcGIS servers (Munger, 2024)
sql/athena/          Common Crawl x .gov queries, ported from the Munger
deploy/              systemd timer, cron line and environment for the weekly cycle
```
