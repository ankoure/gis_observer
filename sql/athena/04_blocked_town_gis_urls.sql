-- GIS-looking URLs that Common Crawl holds for towns our crawler can't reach
-- (crawl_failure = 'blocked': a 403 or Cloudflare's JavaScript challenge). Common
-- Crawl's index sometimes has captures from before the block, and GIS servers on
-- other subdomains (gis.town.gov) are often not behind the same challenge.
--
-- 1. Export the blocked towns' domains and upload them to S3:
--      sqlite3 -csv data/observer.sqlite \
--        "SELECT website FROM jurisdictions WHERE crawl_failure = 'blocked'" > blocked.csv
-- 2. Create the table below (set LOCATION), pick a snapshot, run the query.
-- 3. Load the results; URLs are attributed to their town by host:
--      uv run gis-observer seeds urls <downloaded results>.csv
--      uv run gis-observer probe

CREATE EXTERNAL TABLE IF NOT EXISTS blocked_sites (website STRING)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
LOCATION 's3://YOUR-BUCKET/blocked_sites/';

WITH sites AS (
    SELECT DISTINCT regexp_replace(
        lower(regexp_extract(website, '^(?:https?://)?([^/:]+)', 1)), '^www\.', ''
    ) AS domain
    FROM blocked_sites
)
SELECT DISTINCT c.url AS url, s.domain
FROM ccindex.ccindex c
JOIN sites s ON c.url_host_registered_domain = s.domain
WHERE c.crawl = 'CC-MAIN-2024-22'
  AND c.subset = 'warc'
  AND c.fetch_status = 200
  AND regexp_like(
      lower(c.url_host_name || c.url_path),
      '(^|[^a-z])(gis|arcgis|maps?|mapping|geoportal|geodata|opendata|parcels?)([^a-z]|$)'
  );
