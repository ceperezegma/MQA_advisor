## Purpose

The opt-in `propose` step of the pipeline: it proposes themes and keywords for every Dataset and standalone Data
Service of a catalogue already in the database, saves the valid proposals, and reports every problem in an Excel file
(ADR-0016, ADR-0017, ADR-0019).

## ADDED Requirements

### Requirement: The propose step is opt-in
The pipeline SHALL accept `propose` as a step, run after `db` when selected. The default step list SHALL remain
`manifest,fetch,db`, so `propose` runs only when asked for. `--steps propose` and `--steps db,propose` SHALL work;
`propose` alone SHALL read the catalogue already in the database without running another step.

#### Scenario: Default steps
- **WHEN** the pipeline is run without `--steps`
- **THEN** the `propose` step does not run and no model is called

#### Scenario: Propose alone
- **WHEN** `--steps propose` is given and the catalogue is in the database
- **THEN** only the propose step runs, on the existing rows

#### Scenario: After the load
- **WHEN** `--steps db,propose` is given
- **THEN** the proposals are made after the database load, on the freshly loaded resources

### Requirement: Inputs are checked before any call
Before any model call, the step SHALL check that `ANTHROPIC_API_KEY` is set and that the catalogue has resources in
the database for the selected environment, and SHALL raise `PipelineInputError` otherwise. No call, no row written and
no log file SHALL result from a failed check. A dry run needs no key, since it calls no model.

#### Scenario: Missing key
- **WHEN** the step runs without `ANTHROPIC_API_KEY`, not as a dry run
- **THEN** a `PipelineInputError` is raised before any call and the exit code is 1

#### Scenario: Unknown catalogue
- **WHEN** the catalogue has no resources in the database
- **THEN** a `PipelineInputError` names the catalogue and the step does not start

### Requirement: Per-resource flow and write order
For each resource the step SHALL make the themes call and then the keywords call (the keyword call receives the
effective themes). Calls MAY run concurrently across resources, but the results SHALL be written to the database only
by the main thread, one resource at a time in resource-id order, each replacing that resource's earlier proposals.
The stored proposals SHALL NOT depend on the number of workers.

#### Scenario: Results identical for any number of workers
- **WHEN** the same catalogue is processed with 1 worker and with 4 workers using the same model answers
- **THEN** the stored proposals are identical

#### Scenario: Re-run replaces
- **WHEN** the step runs twice on the same catalogue
- **THEN** each resource has only the proposals of the second run

#### Scenario: Reload removes
- **WHEN** the `db` step reloads the catalogue after a propose run
- **THEN** the proposals are gone and the pipeline logs how many the reload removed

### Requirement: Limit
`--limit N` SHALL restrict the step to the first N resources ordered by resource id. `N` and `--workers` SHALL be
positive integers.

#### Scenario: Limit
- **WHEN** `--limit 3` is given for a catalogue with 10 resources
- **THEN** only the 3 resources with the lowest ids are processed

### Requirement: Dry run
`--dry-run` SHALL build every prompt the run would send, call no model, write no proposal row, create no log file
and not touch the report. It SHALL print the number of calls and a token and cost estimate computed locally from text
length, labelled as rough.

#### Scenario: Nothing is written
- **WHEN** the step runs with `--dry-run`
- **THEN** the proposals table is unchanged, no file is created under `logs/propose/`, and the output shows the number
  of calls and a rough estimate labelled as such

### Requirement: Step options belong to the propose step
`--limit`, `--dry-run` and `--workers` SHALL be rejected as a usage error when `propose` is not among the selected
steps. `--workers` SHALL default to 4.

#### Scenario: Option without the step
- **WHEN** `--dry-run` is given with the default steps
- **THEN** the command fails with a usage error and nothing runs

### Requirement: Every resource is attempted and valid proposals are saved
A failure of one resource SHALL NOT stop the others. Every valid proposal SHALL be saved, also in a run that ends with
errors. A resource skipped for having no title is a warning and SHALL NOT fail the run. A resource whose calls all
failed SHALL keep its earlier proposals.

#### Scenario: One resource fails
- **WHEN** the model call fails for one of three resources
- **THEN** the other two have their proposals saved, the report has an `error` row for the failed one, and the exit
  code is 1

#### Scenario: Skipped resource
- **WHEN** a resource has no title
- **THEN** the report has a `warning` row for it, nothing is stored for it, and the exit code is 0

### Requirement: The report is always written
The step SHALL write `propose_report.xlsx` in the catalogue folder on every non-dry run, even with zero errors. Sheet
*Errors* SHALL have one row per problem (resource type and id, which call, severity `error` or `warning`, message,
prompt name and version, model, UTC time). Sheet *Summary* SHALL give the counts of resources per outcome and of
problems per severity, the call and token totals and the estimated total cost in USD. If any `error` exists the step
SHALL exit 1, after saving the proposals and writing the report. If the report cannot be written, the failures SHALL
still be raised, naming the report problem as well.

#### Scenario: No problems
- **WHEN** every resource succeeds
- **THEN** the report exists with an empty Errors sheet and a Summary with the totals, and the exit code is 0

#### Scenario: Report cannot be written
- **WHEN** an error exists and the report file cannot be written (for example it is open in Excel)
- **THEN** the step still fails with the resource failures, and the message also says the report was not written

### Requirement: Logging
The step SHALL log at INFO one line per resource (calls made, items stored, estimated cost) and, at the end, a run
summary with the totals and the estimated cost. Warnings and errors SHALL be logged at WARNING and ERROR. The per-call
log of the LLM client is written for each non-dry run.

#### Scenario: Run summary
- **WHEN** a non-dry run ends
- **THEN** a summary line with the resource, call and item totals and the estimated cost is logged
