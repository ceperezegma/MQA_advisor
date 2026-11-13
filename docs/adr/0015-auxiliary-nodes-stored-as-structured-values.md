---
status: accepted
---

# Auxiliary nodes are stored inside the value of the property that reaches them

Issue #2. In `nsip-lu`, Dataset `16c923dc-7cf7-11f0-9925-46bf4a153a63` has one `dct:title` and one
`dct:description`, but `property_value` returned two of each: the loader stored every `@graph` node
as an entity, so the title of the `dct:PeriodOfTime` node reached by `dct:temporal` and the
description of the legal resource reached by `dcatap:applicableLegislation` looked like the Dataset's.

## Findings (all fetched `dataset_*.jsonld` files)

- 2,318 Datasets, 11,322 Distributions and 2 embedded Data Services; no node has two core types.
- Auxiliary nodes are at most 2 levels deep (Relationship -> `dcat:hadRole` -> Role).
- 12,949 auxiliary nodes are URIs, not bnodes, so "drop the bnodes" would not have fixed it.
- 5,316 auxiliary nodes are shared by several parents; 10 are reached from no core entity.

## Decision

- Rows exist only for core entities (Dataset, Distribution, Data Service). `entity_kind` replaces
  `entity_type`; the rdf:type rows of a core entity stay. A node reached through `dcat:distribution`
  from a Dataset, or `dcat:accessService` from a Distribution, is core even without a DCAT `@type`
  (the real data has none, this keeps the views from losing one if it ever happens).
- An auxiliary node is stored in the `value` of the property that reaches it, `value_kind = 'node'`,
  as JSON-LD expanded form (W3C format, no invented one): full-URI keys, every value a list,
  `@type` sorted, `@id` kept for a URI node and dropped for a bnode, nested nodes recursive, keys
  sorted when serialised. Reading a property never needs a node id.
- A reference to another core entity (`dcat:distribution`, `dcat:accessService`) stays a `uri` /
  `bnode` row, which the views follow. A reference to an id the graph does not describe stays a
  bare `{"@id": ...}` (`{}` for a bnode).
- A shared auxiliary node is copied into each owner. A node not reached from any core entity is
  not stored; the load counts it (`LoadSummary.orphan_nodes`) and logs one warning.
- Standalone Data Service `publisher` / `contact_point` become node values the same way.
- A file without a `dcat:Dataset` node, with a duplicate `@id`, with a node of two core types, or
  with auxiliary nodes that refer back to each other is malformed and raises.

## Consequences

- Existing databases have the old layout: `connect()` raises and asks to delete `mqa.sqlite` and
  rerun `--steps db` (the fetched files are the source of truth).
- Properties that point at an auxiliary node are read with `json_extract(value, '$."<uri>"[0]."@value"')`.
- A node shared by many owners is stored once per owner.
