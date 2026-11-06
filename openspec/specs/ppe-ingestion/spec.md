# ppe-ingestion Specification

## Purpose
TBD - created by syncing change ppe-metadata-ingestion. Update Purpose after archive.
## Requirements
### Requirement: Catalogue Manifest Loading
Given a catalogue name, the system SHALL load its manifest from `catalogues_to_fetch/{catalogue}.xlsx`, where `{catalogue}` is the xlsx filename without its extension, reading columns `resource_uri`, `type`, and `resource_id`, one resource per row. `type` SHALL be matched case-insensitively against `dataset` and `dataservice`.

#### Scenario: Loading a catalogue manifest
- **WHEN** a catalogue name is given and `catalogues_to_fetch/{catalogue}.xlsx` exists with N rows
- **THEN** the system reads all N rows (`resource_uri`, `type`, `resource_id`), in row order

#### Scenario: Manifest file not found
- **WHEN** a catalogue name is given with no corresponding `catalogues_to_fetch/{catalogue}.xlsx`
- **THEN** the system raises a clear error naming the catalogue and the expected file path, before any network calls are made

### Requirement: Fetch URL Construction
Given a resource `type` and `resource_id`, the system SHALL build the full fetch URL by joining the base URL of the selected environment — the configured `PPE_BASE_URL` (the same base URL used by `ppe-auth`'s `Config`) for `PPE`, `https://data.europa.eu/` for `PROD` — with a type-specific path:
- `dataset` → `api/hub/repo/datasets/{resource_id}.jsonld`
- `dataservice` → `api/hub/search/dataservices/{resource_id}`

No query parameters SHALL be appended to either URL.

#### Scenario: Building a Dataset fetch URL
- **WHEN** a URL is built for `type=dataset`, `resource_id=abc123`, with the environment `PPE` and `PPE_BASE_URL=https://ppe.data.europa.eu/`
- **THEN** the resulting URL is `https://ppe.data.europa.eu/api/hub/repo/datasets/abc123.jsonld`, with no query string

#### Scenario: Building a Data Service fetch URL
- **WHEN** a URL is built for `type=dataservice`, `resource_id=xyz789`, with the environment `PPE` and `PPE_BASE_URL=https://ppe.data.europa.eu/`
- **THEN** the resulting URL is `https://ppe.data.europa.eu/api/hub/search/dataservices/xyz789`, with no `.jsonld` extension and no query string

#### Scenario: Building a Dataset fetch URL on production
- **WHEN** a URL is built for `type=dataset`, `resource_id=abc123`, with the environment `PROD`
- **THEN** the resulting URL is `https://data.europa.eu/api/hub/repo/datasets/abc123.jsonld`, with no query string

#### Scenario: Building a Data Service fetch URL on production
- **WHEN** a URL is built for `type=dataservice`, `resource_id=xyz789`, with the environment `PROD`
- **THEN** the resulting URL is `https://data.europa.eu/api/hub/search/dataservices/xyz789`, with no `.jsonld` extension and no query string

### Requirement: Single Resource Fetch and Persistence
Given a `dataset` or `dataservice` resource's `type` and `resource_id`, and a `force` flag (default `False`), the system SHALL determine whether the expected output file already exists at `fetched_catalogues_data/{env}/{catalogue}/{type}_{resource_id}.{extension}`, where `{env}` is the selected environment (`PPE` or `PROD`) and `{extension}` is `jsonld` for `dataset` and `json` for `dataservice`. If the file already exists and `force` is `False`, the system SHALL skip the fetch entirely — making no network call — and leave the existing file untouched. Otherwise (no existing file, or `force` is `True`), the system SHALL `GET` the resource's fetch URL using the session of the selected environment (the authenticated session from `ppe-auth` for `PPE`, the public session from `ppe-auth` for `PROD`), and on success persist the raw response body unmodified (no JSON parsing) to that path, creating the folder if it does not already exist. An existing file at the same path SHALL be overwritten when the fetch proceeds. Runs for different environments SHALL NOT overwrite each other's files.

#### Scenario: Fetching and persisting a known Dataset
- **WHEN** a `dataset` resource with no existing output file is fetched successfully
- **THEN** its raw response body is written unmodified to `fetched_catalogues_data/{env}/{catalogue}/dataset_{resource_id}.jsonld`

#### Scenario: Fetching and persisting a known standalone Data Service
- **WHEN** a `dataservice` resource with no existing output file is fetched successfully
- **THEN** its raw response body is written unmodified to `fetched_catalogues_data/{env}/{catalogue}/dataservice_{resource_id}.json`

#### Scenario: Existing file skips the fetch by default
- **WHEN** a resource's expected output file already exists on disk and `force` is `False` (the default)
- **THEN** the system makes no network call for that resource, and the existing file is left unchanged

#### Scenario: force=True re-fetches unconditionally
- **WHEN** a resource's expected output file already exists on disk and `force` is `True`
- **THEN** the system fetches the resource anyway and overwrites the existing file with the newly-fetched content

#### Scenario: No existing file is always fetched
- **WHEN** a resource has no existing output file on disk, regardless of the value of `force`
- **THEN** the system fetches the resource normally

#### Scenario: Re-running a catalogue overwrites prior files
- **WHEN** a catalogue is fetched a second time in the same environment with `force=True` after a previous successful run
- **THEN** each resource's file is overwritten with the newly-fetched content, not duplicated or appended

#### Scenario: PPE and PROD runs are stored side by side
- **WHEN** the same catalogue is fetched once with the environment `PPE` and once with `PROD`
- **THEN** the files are written under `fetched_catalogues_data/PPE/{catalogue}/` and `fetched_catalogues_data/PROD/{catalogue}/` respectively, and neither run changes the other's files

#### Scenario: Production fetch needs no login
- **WHEN** a resource is fetched with the environment `PROD`
- **THEN** no PPE login is performed and the fetch uses the public session

### Requirement: Retry on Download Failure
The system SHALL retry a resource fetch that fails to download (any non-success HTTP response or transport-level error) up to a total of 4 attempts (1 initial attempt plus 3 retries), with no delay between attempts, before recording that resource as failed for the current catalogue run.

#### Scenario: Fetch succeeds after one retry
- **WHEN** a resource's first fetch attempt fails but a subsequent attempt (within the 4-attempt budget) succeeds
- **THEN** the resource is persisted normally, with no error recorded for it

#### Scenario: Fetch fails on all 4 attempts
- **WHEN** a resource's fetch fails on all 4 attempts
- **THEN** the resource is recorded as a failed resource for the catalogue run, and no partial or corrupt file is written for it

### Requirement: Standalone Data Service Response Envelope Validation
The Search/Hub API used to fetch `dataservice` resources wraps its response in a `{"status": ..., "result": {...}}` envelope and can return an HTTP success status while the envelope's own `status` field reports failure. The system SHALL treat an HTTP-successful `dataservice` fetch whose response body is not valid JSON, or whose parsed body's `status` field is not `"success"`, as a download failure — subject to the same retry behavior as any other download failure (see Retry on Download Failure) — rather than persisting the response and reporting it as `Success`.

#### Scenario: Envelope reports a non-success status
- **WHEN** a `dataservice` fetch returns HTTP 200 with a body whose `status` field is not `"success"`
- **THEN** the attempt is treated as a failure (counted against the retry budget), and no file is written for that attempt

#### Scenario: Envelope status is success
- **WHEN** a `dataservice` fetch returns HTTP 200 with a body whose `status` field is `"success"`
- **THEN** the response body is persisted verbatim, exactly as for any other successful fetch

#### Scenario: Response body is not valid JSON
- **WHEN** a `dataservice` fetch returns HTTP 200 with a body that cannot be parsed as JSON
- **THEN** the attempt is treated as a failure (counted against the retry budget), and no file is written for that attempt

### Requirement: Bounded Concurrent Fetching
The system SHALL fetch every `dataset` and `dataservice` row in a catalogue manifest concurrently, bounded by a single configurable, global concurrency limit that applies across all fetches in flight for one catalogue run.

#### Scenario: Concurrency limit respected under load
- **WHEN** N rows (N well above the configured concurrency limit), mixing `dataset` and `dataservice` types, are fetched using mocked responses with artificial delay
- **THEN** the number of requests in flight never exceeds the configured concurrency limit at any point, and the total wall-clock time reflects batched concurrent execution, not full serialization

### Requirement: Concurrent-Fetch Failure Isolation
The system SHALL isolate a single resource's exhausted-retry failure so that it does not cancel, block, or discard the results of concurrently-fetching sibling resources.

#### Scenario: One concurrent fetch fails among many successes
- **WHEN** one resource's fetch fails all retry attempts (e.g. a mocked, persistent 404) while many other resources in the same catalogue are being fetched concurrently
- **THEN** all successful sibling resources are still persisted to disk, and the failure is attributed to its specific resource, not merged into or blocking the others

### Requirement: Safe Session Reuse Under Concurrency
The system SHALL reuse the single authenticated session from `ppe-auth` across concurrent fetches without a race condition re-authenticating or corrupting shared session state; login happens once, single-threaded, before any concurrent fetching begins.

#### Scenario: Many concurrent fetches share one session
- **WHEN** many resources are fetched concurrently using the same authenticated session
- **THEN** no re-authentication is triggered by the concurrent fetches themselves, and no response data is corrupted or interleaved between concurrent requests

### Requirement: Post-Run Download Report
After all rows in a catalogue's manifest have been attempted, the system SHALL verify each row's actual file presence on disk (ground truth, independent of the in-process fetch result, and independent of whether that file came from a fetch performed during this run or was skipped as already-present) and write a report to `fetched_catalogues_data/{env}/{catalogue}/report.xlsx` with columns `resource_uri`, `type`, `resource_id`, `file_name`, and `download_result`, one row per manifest entry in manifest order. `download_result` SHALL be `Success` (file confirmed present) or `Failed` (file confirmed absent after a fetch was attempted) for both `dataset` and `dataservice` rows. `file_name` SHALL be the resource's expected filename (`{type}_{resource_id}.{extension}`, with `{extension}` being `jsonld` for `dataset` and `json` for `dataservice`) regardless of whether it was actually produced.

#### Scenario: Report reflects actual disk state, not in-process belief
- **WHEN** the report is generated for a catalogue run
- **THEN** each row's `download_result` is determined by whether its expected file is actually found on disk at report-generation time, not merely by whether the fetch call itself returned success

#### Scenario: Mixed outcomes in one report
- **WHEN** a catalogue manifest has some successfully-fetched `dataset` rows, some `dataset` rows that failed all retries, and some `dataservice` rows fetched successfully
- **THEN** the report contains one row per manifest entry, with `download_result` of `Success` or `Failed` as appropriate, and rows are not lost or reordered relative to the manifest

#### Scenario: Partial catalogue failure preserves prior results and report accuracy
- **WHEN** fetching a catalogue manifest, one row's fetch fails all retries partway through the run
- **THEN** the files already persisted for other rows are not lost, and the report still names exactly which resource failed

#### Scenario: A resumed run reports skipped-because-already-fetched resources as Success
- **WHEN** a catalogue is re-run with `force=False` and some resources are skipped because their expected files already exist on disk
- **THEN** those resources' `download_result` is `Success`, identical to a resource freshly fetched during this run

#### Scenario: A resource missing in the selected environment is reported as Failed
- **WHEN** a manifest row's resource does not exist in the selected environment and all fetch attempts fail
- **THEN** its `download_result` is `Failed` in that environment's report, and the other rows are unaffected

### Requirement: Catalogue-Level Resumability
`PPEIngestor.run_catalogue` SHALL accept a `force` flag (default `False`) and apply it uniformly to every fetchable row's fetch, so that a re-run of a catalogue with `force=False` fetches only the resources whose expected files are not yet present on disk, leaving already-fetched resources untouched and making no network call for them.

#### Scenario: Resuming a partially-fetched catalogue only fetches what's missing
- **WHEN** a catalogue is run with `force=False` and some, but not all, of its resources already have their expected output files on disk from a prior run
- **THEN** only the resources without an existing file are fetched over the network; the rest are left as-is with no network call made for them

#### Scenario: force=True re-fetches an entire catalogue
- **WHEN** a catalogue is run with `force=True`
- **THEN** every fetchable row is fetched over the network and its file overwritten, regardless of whether it already existed on disk

### Requirement: Ingestion Environment Matches Its Session
The environment an ingestion run is labelled and stored under SHALL never disagree with the host actually fetched from. When no environment is given, the system SHALL derive it from the session in use (the public session is `PROD`, the authenticated PPE session is `PPE`). When an explicit environment contradicts the session in use, the system SHALL raise a configuration error before any network call.

#### Scenario: Environment defaults from the session
- **WHEN** an ingestor is created with a public session and no environment, or with an authenticated PPE session and no environment, regardless of `MQA_ENV`
- **THEN** its environment is `PROD` for the public session and `PPE` for the PPE session

#### Scenario: Contradicting environment is rejected
- **WHEN** an ingestor is created with a public session and the environment `PPE`, or with an authenticated PPE session and the environment `PROD`
- **THEN** the system raises a configuration error before any network call
