---
status: accepted
supersedes: part of ADR-0012 (the PROD-cache-vs-PPE-ingestion warning)
---

# Metadata ingestion follows `MQA_ENV`, using production's public endpoints

ADR-0012 made the MQA cache (Reference Results) switchable between PPE and production but left
metadata ingestion on PPE, so a `PROD` Test Run compared our scores of PPE metadata with
production's MQA scores. Issue #45 closes that gap.

## Findings (checked live)

- Production's `/api/hub/repo/datasets/{id}.jsonld` and `/api/hub/search/dataservices/{id}` are
  **public**: HTTP 200 with no login. Only PPE sits behind the Cognito flow (ADR-0007).
- No JSON-LD normalisation is needed for production: scoring already handles its flat `@graph`
  output. Some PPE datasets are serialised without `@graph` and cannot be scored; that is a
  pre-existing PPE-side inconsistency and is not addressed here.

## Decision

- `MQA_ENV` (`PPE` | `PROD`, default `PROD`) now drives ingestion as well as the MQA cache. The
  environment is resolved once per Test Run, before any login, and shared by ingestion, the
  fetcher and every output folder (`environment.py`).
- `PROD` ingestion uses a plain `PublicSession` (`get`, `base_url=https://data.europa.eu/`): no
  login, and `PPE_USERNAME`/`PPE_PASSWORD` are not required. `PPE` is unchanged. `PPEIngestor`
  only needs `get` and `base_url` from its session, so it accepts either unchanged.
- Fetched metadata, own-score exports and Discrepancy reports are stored per environment
  (`fetched_catalogues_data|fetched_data_scoring|MQA_discrepancy_tests / {PPE|PROD} / {catalogue}`),
  alongside `MQA_results/{PPE|PROD}/` from ADR-0012. Manifests stay shared between environments.
- The PROD-cache-vs-PPE-ingestion warning from ADR-0012 is removed.

## Consequences

- A default run no longer touches PPE or needs PPE credentials; `MQA_ENV=PPE` restores the
  PPE-only behaviour.
- Output paths gained the environment level; existing data was moved into the `PPE/` folders.
- A manifest resource missing in the selected environment is reported `Failed` in that
  environment's ingestion report, as before.
- An injected ingestor and fetcher with different environments are not cross-checked; only the
  default construction path guarantees they agree.
