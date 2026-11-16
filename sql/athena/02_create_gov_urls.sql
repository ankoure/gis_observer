-- CISA's registry of .gov domains, uploaded to S3 as CSV.
-- Source: https://github.com/cisagov/dotgov-data (current-full.csv)
-- Ported from ArcGISRest_Munger/Athena/CreateGovURLs.sql. The CSV's columns have
-- changed since 2024: "Agency" is gone and "Suborganization name" was added.
-- Set LOCATION to the folder you upload current-full.csv to.

CREATE EXTERNAL TABLE IF NOT EXISTS gov_urls (
    domain_name            STRING,
    domain_type            STRING,
    organization_name      STRING,
    suborganization_name   STRING,
    city                   STRING,
    state                  STRING,
    security_contact_email STRING
)
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
LOCATION 's3://YOUR-BUCKET/dotgov/'
TBLPROPERTIES ('skip.header.line.count' = '1');
