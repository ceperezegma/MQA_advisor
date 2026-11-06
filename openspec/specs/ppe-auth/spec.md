# ppe-auth Specification

## Purpose
TBD - created by archiving change ppe-authentication. Update Purpose after archive.
## Requirements
### Requirement: Session Authentication from .env Credentials
The system SHALL obtain an authenticated PPE session in a single call, using credentials read from `.env`, with no interactive/manual login step required.

#### Scenario: Successful login
- **WHEN** the auth module is invoked with valid `.env` credentials
- **THEN** it returns a session object usable for subsequent Registry API requests

#### Scenario: Login authorizes a real Registry API call
- **WHEN** the returned session performs a `GET` against a Registry API resource (e.g. `/api/hub/repo/datasets/{id}.jsonld`)
- **THEN** the response is a 200 with JSON-LD content, not a redirect to the login page

### Requirement: Session Reuse Across Requests
The system SHALL reuse a single authenticated session across multiple Registry API requests without re-authenticating per resource.

#### Scenario: Fetching multiple resources
- **WHEN** N resources are fetched in a row using the same session object
- **THEN** the login flow executes only once, and all N requests reuse that session

### Requirement: Session Expiry Detection
The system SHALL detect an expired or invalid session and raise a distinguishable error to calling code, rather than returning a silent empty or malformed response.

#### Scenario: Expired session on a Registry API call
- **WHEN** a request using a previously-valid session receives a 401 response or is redirected to the login page
- **THEN** the auth module raises a specific "session expired/invalid" error rather than returning the response as-is

### Requirement: Startup Credential Validation
When the selected environment is `PPE`, the system SHALL validate that required `.env` credentials are present and non-empty at startup, before any network call is attempted, and SHALL raise a clear configuration error if they are missing or malformed. When the selected environment is `PROD`, no credentials are required and none SHALL be validated.

#### Scenario: Missing credentials
- **WHEN** the selected environment is `PPE` and `PPE_USERNAME` or `PPE_PASSWORD` is missing or empty in the environment
- **THEN** the system raises a clear configuration error at startup, before attempting any login request

#### Scenario: Missing credentials are fine for production
- **WHEN** the selected environment is `PROD` and `PPE_USERNAME` and `PPE_PASSWORD` are missing
- **THEN** no configuration error is raised and no login is attempted

### Requirement: Credential Confidentiality
The system SHALL never include raw credential values in log output or exception messages.

#### Scenario: Login failure is logged
- **WHEN** a login attempt fails (e.g. wrong password, network error) and the failure is logged or raised
- **THEN** the log/exception message does not contain the raw username or password value

### Requirement: Public Session for Production
The system SHALL provide an unauthenticated session for the production environment that offers the same interface the ingestion code uses from the authenticated session (a `get(url)` call and a `base_url`), with `base_url` set to `https://data.europa.eu/`. It SHALL perform no login, SHALL NOT read `PPE_USERNAME` or `PPE_PASSWORD`, and SHALL NOT send any credentials.

#### Scenario: Public session needs no credentials
- **WHEN** a public session is created and used while `PPE_USERNAME` and `PPE_PASSWORD` are unset
- **THEN** it works and no configuration error is raised

#### Scenario: Public session fetches without logging in
- **WHEN** `get(url)` is called on a public session
- **THEN** a plain HTTP GET is made to that URL with no authentication step beforehand, and its response is returned as-is

#### Scenario: Public session points at production
- **WHEN** a public session is created
- **THEN** its `base_url` is `https://data.europa.eu/`

#### Scenario: Public session requests cannot hang forever
- **WHEN** `get(url)` is called on a public session without an explicit timeout
- **THEN** a default timeout is applied to the request, and an explicit timeout given by the caller takes precedence
