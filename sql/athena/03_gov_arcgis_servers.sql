-- ArcGIS REST services directories on registered .gov domains, from one Common Crawl
-- snapshot. Ported from ArcGISRest_Munger (GetGovArcGISServer.sql + RegEx_Rest_Services.sql),
-- with the root extraction done here instead of in Postgres afterwards.
--
-- List snapshots:  SELECT DISTINCT crawl FROM ccindex.ccindex ORDER BY 1 DESC;
-- Load results:    uv run gis-observer seeds urls <downloaded results>.csv
--
-- Registered .gov only. Many towns use .org/.com/.us domains; the OSM seeds and the
-- site crawler cover those.

SELECT DISTINCT
    regexp_extract(c.url, '^(https?://[^/]+(?:/[^/]+)*?/rest/services)', 1) AS url,
    c.url_host_registered_domain AS domain
FROM ccindex.ccindex c
WHERE c.crawl = 'CC-MAIN-2024-22'
  AND c.subset = 'warc'
  AND c.url_host_registered_domain IN (SELECT lower(domain_name) FROM gov_urls)
  AND lower(c.url_path) LIKE '%/rest/services%';
