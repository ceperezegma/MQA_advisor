---
status: proposed
---

# Standalone Data Service metadata is fetched via the Search/Hub API, not the Registry API

ADR-0003 fetches every other resource type (Dataset today; Distributions come inline with
their parent Dataset) via PPE's Registry API, `{base}api/hub/repo/{type}/{id}.jsonld` —
content-negotiated DCAT-AP JSON-LD, authenticated by the same session cookie as the rest of
the portal. `ingest.py` already has an untested `dataservice` branch pointing at a different
API (`{base}api/hub/search/dataservices/{id}`, no `.jsonld` suffix) — added when standalone
Data Service ingestion was first stubbed out (issue #2) but never exercised, since no
manifest has ever had a `dataservice` row and `run_catalogue` only submits `dataset` rows.

Tested live against PPE (authenticated, using a real standalone Data Service id observed in
a hand-fetched sample file) to confirm which of the two is actually correct before building
on either:

| URL pattern | Result |
|---|---|
| `{base}api/hub/repo/dataservices/{id}.jsonld` (Registry API, matching every other type) | 404 |
| `{base}api/hub/search/dataservices/{id}` (Search/Hub API) | 200, `application/json` |

So the Registry API does not serve standalone Data Services at all on PPE — the branch
`ingest.py` already had is the only one that works, and it's the Search/Hub API, not the
Registry API. The response is also a materially different shape from every other fetched
resource: not JSON-LD (`@id`/`@type`/`@graph`, DCAT-AP property URIs as object keys), but a
pre-flattened `{"status": ..., "result": {...}}` envelope with its own field names
(`endpoint_url`, `access_right`, `conforms_to`, ...) and its own value conventions (a
URI-valued field wraps as `{"resource": "..."}`, not `{"@id": "..."}`).

**Decision:** keep fetching standalone Data Services via the Search/Hub API (confirmed
working). Scoring will still reuse `scoring.py`'s existing JSON-LD-reading code
(`score_resource`/`JsonLdGraph`) completely unmodified — the same code Datasets/
Distributions/embedded Data Services already use — but only after a new, small adapter first
translates the Search API's flat shape into a synthetic JSON-LD node matching what that code
already expects. Datasets/Distributions/embedded Data Services need no such translation,
since PPE already returns them as JSON-LD directly; a standalone Data Service is the one
resource type that does. See `docs/design-standalone-dataservices.md` (Part B, option 1) for
that adapter's design and its full, one-row-per-Property field mapping.

**Consequence beyond the translation step:** the Search API's own field set does not
obviously cover every Data Service Property this project scores. Checking all 18
`catalog.data_service` Properties (from `MQA-properties-for-app.xlsx`'s Dataservices sheet)
against both sample files found no field corresponding to Publisher (`dct:publisher`,
Recommended, 0.5 weight) — no top-level key in either sample's `result` object maps to it.
Unlike the shape mismatch above, this isn't something a translation adapter can fix: if the
Search API genuinely never exposes Publisher for a standalone Data Service, this Property
scores `0` for every one regardless of the adapter's correctness, and any resulting
Discrepancy against the MQA App's own score is structural, not a bug in this project's own
comparison logic. Confirming whether that gap is real (vs. a field we haven't found yet, e.g.
nested under `catalog`) is an open question before Part B implementation — see the design
doc's Part B open questions.

This mirrors ADR-0009's precedent (translate via a mapping layer rather than force a second
data source through the same code path as the first) but for a different reason: ADR-0009's
mapping bridges two different *vocabularies* for the same JSON-LD-shaped data; this one
bridges two structurally different *response shapes* for the same conceptual resource.
