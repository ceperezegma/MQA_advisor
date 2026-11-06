"""How the keys of a standalone Data Service JSON record map to properties.

A standalone Data Service is fetched as the EDP API envelope `{"status": ..., "result": {...}}`,
not JSON-LD, so it has no `@context`. This module is the one place that says what each key of
`result` becomes. Properties are written with short prefixed names and expanded to full URIs
once, at import; a prefix missing from `PREFIXES` makes the import fail.

Every key of `result` is in exactly one of the tables below or in `IGNORED_KEYS`; the loader
raises on any other key, so a new key in the API can never be dropped silently.
"""

from __future__ import annotations

from dataclasses import dataclass

PREFIXES = {
    "dct": "http://purl.org/dc/terms/",
    "dcat": "http://www.w3.org/ns/dcat#",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "vcard": "http://www.w3.org/2006/vcard/ns#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcatap": "http://data.europa.eu/r5r/",
}


def expand(name: str, prefixes: dict[str, str] = PREFIXES) -> str:
    """Expand `prefix:local` to a full URI; an undefined prefix raises a `ValueError`."""
    prefix, sep, local = name.partition(":")
    if not sep or prefix not in prefixes:
        raise ValueError(f"undefined prefix {prefix!r} in {name!r}")
    return prefixes[prefix] + local


RDF_TYPE = expand("rdf:type")
DATA_SERVICE_TYPE = expand("dcat:DataService")

# `id` is the resource's `resource_id`, not a property.
ID_KEY = "id"

# {"<lang>": "text"}: a literal per language, `lang` taken from the key as delivered.
LANGUAGE_MAP_KEYS = {
    "title": expand("dct:title"),
    "description": expand("dct:description"),
}

# [{"label": "text", "language": "<lang>"}]: a literal per item (`id` is dropped).
KEYWORDS_KEY = "keywords"
KEYWORDS_PROPERTY = expand("dcat:keyword")

# ["uri", ...]: a `uri` row per item.
URI_LIST_KEYS = {
    "endpoint_url": expand("dcat:endpointURL"),
    "endpoint_description": expand("dcat:endpointDescription"),
    "applicable_legislation": expand("dcatap:applicableLegislation"),
    "serves_dataset": expand("dcat:servesDataset"),
}

# {"resource": "uri", ...}: one `uri` row from `resource` (`id` and `label` are dropped).
RESOURCE_KEYS = {
    "access_right": expand("dct:accessRights"),
    "format": expand("dct:format"),
    "license": expand("dct:license"),
}

# [{"resource": "uri", ...}]: a `uri` row per item's `resource` (`id` and `label` are dropped).
RESOURCE_LIST_KEYS = {
    "theme": expand("dcat:theme"),
    "hvd_category": expand("dcatap:hvdCategory"),
    "landing_page": expand("dcat:landingPage"),
}


@dataclass(frozen=True)
class NestedNode:
    """A nested object that becomes its own entity, referenced from the root.

    `types` maps the object's JSON `type` to the entity type; `fields` maps its other keys to
    `(property, value_kind)`. `many` says whether the key holds a list of objects.
    """

    reference: str
    types: dict[str, str]
    fields: dict[str, tuple[str, str]]
    many: bool
    id_prefix: str


NESTED_KEYS = {
    "publisher": NestedNode(
        reference=expand("dct:publisher"),
        types={
            "Organization": expand("foaf:Organization"),
            "Agent": expand("foaf:Agent"),
        },
        fields={
            "name": (expand("foaf:name"), "literal"),
            "email": (expand("foaf:mbox"), "uri"),
            "homepage": (expand("foaf:homepage"), "uri"),
        },
        many=False,
        id_prefix="_:publisher",
    ),
    "contact_point": NestedNode(
        reference=expand("dcat:contactPoint"),
        types={"Kind": expand("vcard:Kind")},
        fields={"email": (expand("vcard:hasEmail"), "uri")},
        many=True,
        id_prefix="_:contact_point",
    ),
}

# Metadata or content that is not a property: the catalogue is already a column, the rest is
# translation bookkeeping or reference-result content.
IGNORED_KEYS = frozenset({"catalog", "translation_meta", "count", "results"})

MAPPED_KEYS = frozenset(
    {ID_KEY, KEYWORDS_KEY}
    | LANGUAGE_MAP_KEYS.keys()
    | URI_LIST_KEYS.keys()
    | RESOURCE_KEYS.keys()
    | RESOURCE_LIST_KEYS.keys()
    | NESTED_KEYS.keys()
)

# Guard against a key listed in two tables, which would make its meaning ambiguous.
assert not MAPPED_KEYS & IGNORED_KEYS
