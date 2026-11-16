## ADDED Requirements

### Requirement: Structured model call from a loaded prompt
The system SHALL provide an `LLMClient` that makes one model call from a `LoadedPrompt` and returns the output
parsed from a forced tool call. The tool's input schema SHALL be `output_schema(prompt.name, prompt.version)`,
the call SHALL use the prompt's `model`, `temperature` and `max_tokens`, the prompt's rendered system part SHALL
be sent as the system message and its rendered user part as the single user message. The result SHALL contain the parsed output, input tokens,
output tokens, cached input tokens, estimated cost in USD, latency in seconds and the attempt number (1 when the
first request succeeded).

#### Scenario: Successful call
- **WHEN** the fake backend answers a `propose_themes` v1 call with `{"themes": ["ENVI"]}`
- **THEN** the result's output is `{"themes": ["ENVI"]}` and it carries the token counts, cost, latency and attempt

#### Scenario: System and user parts are sent separately
- **WHEN** a prompt with a system part and a user part is called
- **THEN** the backend request carries the system text as `system` and the user text as the user message

#### Scenario: Response does not match the schema
- **WHEN** the backend answers with `{"themes": []}` (violates `minItems`), a missing required key, a wrong type,
  an extra key or a `language` that does not match its pattern (validated with `jsonschema`)
- **THEN** the client raises `LLMResponseError` carrying the raw response, and the raw response is written to the
  run log

### Requirement: SDK retries and timeout
The real backend SHALL use the `anthropic` SDK with `max_retries=3` and a 60 second timeout. Each retried
transient error SHALL be logged at WARNING with the attempt number. After the SDK's retries are exhausted the
error SHALL propagate as `LLMCallError` (chained to the SDK error).

#### Scenario: Client configuration
- **WHEN** the real backend is built
- **THEN** the SDK client has `max_retries` 3 and timeout 60 seconds

#### Scenario: Retried error is logged
- **WHEN** a request is answered with a transient error status and then succeeds
- **THEN** a WARNING with the attempt number is logged and the result's attempt number is 2

#### Scenario: Retries exhausted
- **WHEN** the backend keeps failing
- **THEN** `LLMCallError` is raised and the failed call is logged

### Requirement: API key from the environment
The system SHALL read `ANTHROPIC_API_KEY` from the environment (loaded from `.env`). When it is missing or blank,
building the real client SHALL raise a clear input error before any call is made. The key SHALL never appear in a
log message or in the log file. `.env.example` SHALL contain an `ANTHROPIC_API_KEY=` placeholder.

#### Scenario: Missing key
- **WHEN** `ANTHROPIC_API_KEY` is unset or blank and the real client is built
- **THEN** the input error is raised and no request is made

#### Scenario: Key never logged
- **WHEN** a call is made with the key `sk-test-secret`
- **THEN** that string appears in neither the log file nor any captured log record

### Requirement: Estimated cost from a committed price table
The system SHALL keep a committed price table: USD per million tokens for input, output and cached input, per
model, with an `as_of` date. The cost of a call SHALL be `(uncached input tokens x input price + cached tokens x
cached price + output tokens x output price) / 1,000,000`, where uncached input tokens are the input tokens not
served from the cache. A model that is not in the table SHALL raise an error naming the model, before any call.
The cost SHALL be described as an estimate.

#### Scenario: Cost arithmetic
- **WHEN** a call uses 1,000 uncached input, 200 cached and 500 output tokens
- **THEN** the cost equals the sum of each count times its per-million price

#### Scenario: Unknown model
- **WHEN** a prompt names a model that is not in the price table
- **THEN** an error naming the model is raised and the backend is not called

#### Scenario: Price table carries its date
- **WHEN** the price table is read
- **THEN** it has an `as_of` date in ISO format and an entry for `claude-haiku-5-5` and `claude-sonnet-5-5`

### Requirement: Fake backend for tests
The system SHALL define a backend interface that the real SDK backend and a fake both implement. The fake SHALL
return scripted responses (or raise scripted errors) and record each request, so unit tests never touch the
network.

#### Scenario: Fake records requests
- **WHEN** a client with the fake backend makes two calls
- **THEN** the fake holds both requests, in order, with model, user text and schema

### Requirement: Per-run interaction log
The system SHALL write one plain-text log file per run at `logs/propose/{catalogue}-{UTC timestamp}.log` through
the standard `logging` module, creating the folder if needed. Two runs SHALL create two different files. For each
call the file SHALL contain one delimited block with: run id, resource, prompt name, version and sha256, model,
the exact system text and user text sent, the raw response, the validation outcome, input/output/cached tokens,
estimated cost, latency and attempt number. An INFO line per call SHALL carry its cost, and the run total SHALL be
logged when the run ends. A dry run SHALL create no log file. `logs/` SHALL be git-ignored.

#### Scenario: Block contains every field
- **WHEN** a call completes
- **THEN** the log file has one block containing all the listed fields

#### Scenario: Two runs, two files
- **WHEN** two runs of the same catalogue start at different times
- **THEN** two files with different names exist under `logs/propose/`

#### Scenario: INFO line per call
- **WHEN** a call completes
- **THEN** one INFO record is logged that contains the resource and the estimated cost of that call

#### Scenario: Failed call is logged
- **WHEN** a call ends in `LLMCallError` or `LLMResponseError`
- **THEN** the file has a block for it with the same fields (response and tokens as far as known) and the
  error outcome, and the call's cost counts as 0 in the run total unless tokens were reported

#### Scenario: Run total
- **WHEN** a run with two calls ends
- **THEN** the log contains the total cost, equal to the sum of the two call costs

#### Scenario: Dry run writes no file
- **WHEN** the run log is opened in dry-run mode
- **THEN** no file or folder is created
