----
-- resource information about the 'nsip-lu' catalogue: 51 datasets + 1 dataservice
select *
from resource
where catalogue = '52north' --''nsip-lu';

-- Getting info about the resources properties that we need to generate improvement proposals:
-- `dcat:keyword`, `dcat:theme`
select r.resource_pk,
       r.resource_type,
       r.resource_id,
       p.property_uri,
       p.value,
       p.value_kind,
       p.datatype,
       p.lang
from resource r
join property_value p on r.resource_pk = p.resource_pk
                                    and r.environment = 'PROD'
                                    and r.catalogue = 'nsip-lu'
where (p.property_uri like '%theme%'
        or p.property_uri like '%keyword%'
        or p.property_uri like '%title%'
        or p.property_uri like '%description%')
    and r.resource_type in ('dataset', 'dataservice')
order by r.resource_id, p.property_uri;


-- ANECDOTE: Properties for a single resource: 16c923dc-7cf7-11f0-9925-46bf4a153a63
select r.resource_pk,
       r.resource_type,
       r.resource_id,
       p.property_uri,
       p.value,
       p.value_kind,
       p.datatype,
       p.lang
from resource r
join property_value p on r.resource_pk = p.resource_pk
                                    and r.environment = 'PROD'
                                    and r.catalogue = 'nsip-lu'
where r.resource_id = '16c923dc-7cf7-11f0-9925-46bf4a153a63'
    and (p.property_uri like '%theme%'
            or p.property_uri like '%keyword%'
            or p.property_uri like '%title%'
            or p.property_uri like '%description%')
order by r.resource_id, p.property_uri;


-- ANECDOTE: Looking at all themes in the nsip-lu catalogue
-- to check whether they are nodes
select r.resource_pk,
       r.resource_type,
       r.resource_id,
       p.property_uri,
       p.value,
       p.value_kind,
       p.datatype,
       p.lang
from resource r
join property_value p on r.resource_pk = p.resource_pk
                                    and r.environment = 'PROD'
                                    and r.catalogue = 'nsip-lu'
where p.property_uri like '%theme%'
order by r.resource_id, p.property_uri;

-- Looking at the dataservice in the catalogue
select r.resource_pk,
       r.resource_type,
       r.resource_id,
       p.property_uri,
       p.value,
       p.value_kind,
       p.datatype,
       p.lang
from resource r
join property_value p on r.resource_pk = p.resource_pk
                                    and r.environment = 'PROD'
                                    and r.catalogue = 'nsip-lu'
where r.resource_type = 'dataservice'
order by r.resource_id, p.property_uri;


