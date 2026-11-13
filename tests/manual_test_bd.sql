---------------------------------
-- TEST DATASETS & DATASERVICES
---------------------------------

-- Datasets: List all properties extracted from a dataset (resource_pk = 1)
select r.*,
    p.*
from  resource r
join property_value p on r.resource_pk = p.resource_pk
                                    and r.resource_pk = 1
-- where r.resource_id = '16c923dc-7cf7-11f0-9925-46bf4a153a63'
--     and p.entity_id = 'http://data.europa.eu/88u/dataset/16c923dc-7cf7-11f0-9925-46bf4a153a63'
--     and p.property_uri like '%keyword%'
order by p.entity_kind, p.property_uri;

-- Dataservices: List all properties extracted from a dataset (resource_pk = 1)
select r.*,
    p.*
from  resource r
join property_value p on r.resource_pk = p.resource_pk
                                    and r.resource_pk = 1
order by p.entity_kind, p.property_uri;



--------------------
-- 🚨 CLEAN TABLES
--------------------
-- Check before delete
select (select count(*) from resource) as resources,
       (select count(*) from property_value) as property_value;

-- Delete all records in both tables
DELETE FROM property_value;
DELETE FROM resource;

-- Check delete
select (select count(*) from resource) as resources,
       (select count(*) from property_value) as property_value;