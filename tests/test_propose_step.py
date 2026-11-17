"""Tests for the `propose` pipeline step: the run, the report, the dry run and the pipeline wiring.

No network: the model is a scripted backend and the data is a small catalogue written here and loaded with the
real loader. Nothing is written outside `tmp_path`.
"""

import functools
import json
import logging
import threading

import openpyxl
import pytest

from mqa_advisor import pipeline, propose_step
from mqa_advisor.catalogue_db import connect, load_catalogue
from mqa_advisor.llm import BackendResponse, LLMCallError, LLMInputError
from mqa_advisor.propose import OWN_DATASET_PREFIX
from mqa_advisor.propose_step import (
    REPORT_NAME,
    ProposeIncompleteError,
    ProposeSummary,
    run_propose,
)

CONTEXT = {"dct": "http://purl.org/dc/terms/", "dcat": "http://www.w3.org/ns/dcat#"}
AGRI = "http://publications.europa.eu/resource/authority/data-theme/AGRI"


class ScriptedBackend:
    """Answers by prompt: theme AGRI, keywords alpha and beta; fails when the user text has a marker."""

    def __init__(self, fail_on=()):
        self.fail_on = fail_on
        self.requests = []
        self._lock = threading.Lock()
        self.closed = False

    def create(self, request):
        with self._lock:
            self.requests.append(request)
        if any(marker in request.user for marker in self.fail_on):
            raise LLMCallError("the API is down")
        if request.tool_name == "propose_themes":
            output = {"themes": ["AGRI"]}
        else:
            output = {"keywords": ["alpha", "beta"], "language": "eng"}
        return BackendResponse(output, "{}", 100, 0, 10)

    def close(self):
        self.closed = True


def _write_dataset(folder, name, title):
    node = {"@id": OWN_DATASET_PREFIX + f"dataset/{name}", "@type": "dcat:Dataset"}
    if title:
        node["dct:title"] = {"@value": title, "@language": "eng"}
    (folder / f"dataset_{name}.jsonld").write_text(
        json.dumps({"@context": CONTEXT, "@graph": [node]}), encoding="utf-8"
    )


@pytest.fixture
def data_dir(tmp_path):
    """`fetched/PROD/cat` with d1 and d2 (titled) and d3 (no title)."""
    folder = tmp_path / "fetched" / "PROD" / "cat"
    folder.mkdir(parents=True)
    _write_dataset(folder, "d1", "Title d1")
    _write_dataset(folder, "d2", "Title d2")
    _write_dataset(folder, "d3", None)
    return tmp_path / "fetched"


@pytest.fixture
def conn(tmp_path, data_dir):
    connection = connect(tmp_path / "mqa.sqlite")
    load_catalogue("PROD", "cat", conn=connection, data_dir=data_dir)
    yield connection
    connection.close()


@pytest.fixture
def go(tmp_path, data_dir, conn):
    def _go(backend=None, **kwargs):
        backend = backend or ScriptedBackend()
        return run_propose(
            "PROD",
            "cat",
            conn=conn,
            fetched_data_dir=data_dir,
            backend_factory=lambda: backend,
            log_dir=tmp_path / "logs",
            **kwargs,
        )

    return _go


def stored(conn):
    """The stored proposals as comparable tuples (without the timestamp), ordered."""
    return conn.execute(
        "SELECT r.resource_id, p.property_uri, p.value, p.value_kind, p.lang, p.prompt_name, p.model"
        " FROM proposed_property_value p JOIN resource r USING (resource_pk)"
        " ORDER BY r.resource_id, p.property_uri, p.rowid"
    ).fetchall()


def sheet_rows(path, name):
    workbook = openpyxl.load_workbook(path, read_only=True)
    try:
        return [list(row) for row in workbook[name].iter_rows(values_only=True)]
    finally:
        workbook.close()


def summary_of(path):
    return {row[0]: row[1] for row in sheet_rows(path, "Summary")[1:]}


# --- the run ------------------------------------------------------------------------------------


def test_stores_themes_and_keywords_and_warns_about_a_resource_without_title(
    go, conn, data_dir
):
    summary = go()

    rows = stored(conn)
    assert [r[0] for r in rows] == ["d1"] * 3 + [
        "d2"
    ] * 3  # d3 has no title: nothing stored
    themes = [r for r in rows if r[1].endswith("#theme")]
    keywords = [r for r in rows if r[1].endswith("#keyword")]
    assert [(r[0], r[2], r[3], r[4]) for r in themes] == [
        ("d1", AGRI, "uri", None),
        ("d2", AGRI, "uri", None),
    ]
    assert [(r[0], r[2], r[3], r[4]) for r in keywords] == [
        ("d1", "alpha", "literal", "eng"),
        ("d1", "beta", "literal", "eng"),
        ("d2", "alpha", "literal", "eng"),
        ("d2", "beta", "literal", "eng"),
    ]
    assert {r[5] for r in rows} == {"propose_themes", "propose_keywords"}
    assert (summary.resources, summary.stored, summary.skipped, summary.failed) == (
        3,
        2,
        1,
        0,
    )
    assert (summary.themes, summary.keywords, summary.calls) == (2, 4, 4)
    assert (summary.errors, summary.warnings) == (
        0,
        1,
    )  # the "no title" warning, listed once


def test_a_second_run_replaces_the_proposals(go, conn):
    go()
    first = stored(conn)
    go()
    assert stored(conn) == first


def test_limit_takes_the_first_resources_by_id(go, conn):
    summary = go(limit=1)
    assert summary.resources == 1
    assert {r[0] for r in stored(conn)} == {"d1"}


def test_results_are_identical_for_one_and_four_workers(go, conn):
    go(workers=1)
    with_one = stored(conn)
    conn.execute("DELETE FROM proposed_property_value")
    conn.commit()
    go(workers=4)
    assert stored(conn) == with_one


def test_invalid_workers_or_limit_are_rejected(go):
    with pytest.raises(ValueError):
        go(workers=0)
    with pytest.raises(ValueError):
        go(limit=0)


def test_the_backend_is_closed_and_the_run_log_is_written(go, tmp_path):
    backend = ScriptedBackend()
    go(backend)
    assert backend.closed
    [log] = (tmp_path / "logs").iterdir()
    assert "total: 4 call(s)" in log.read_text(encoding="utf-8")


def test_the_run_logs_each_resource_and_a_summary(go, caplog):
    with caplog.at_level(logging.INFO, logger="mqa_advisor.propose_step"):
        go()
    assert "resource d1: 2 call(s), 3 item(s) stored" in caplog.text
    assert "3 resource(s): 2 with proposals, 1 skipped, 0 failed" in caplog.text
    assert "estimated cost USD" in caplog.text


# --- failures and the report --------------------------------------------------------------------


def test_one_failing_resource_saves_the_others_reports_and_raises(go, conn, data_dir):
    with pytest.raises(ProposeIncompleteError) as raised:
        go(ScriptedBackend(fail_on=("Title d2",)))

    assert {r[0] for r in stored(conn)} == {"d1"}
    error = raised.value
    assert [(f.resource_id, f.call, f.severity) for f in error.failures] == [
        ("d2", "themes", "error"),
        ("d2", "keywords", "error"),
    ]
    assert error.report_path == data_dir / "PROD" / "cat" / REPORT_NAME
    errors = sheet_rows(error.report_path, "Errors")
    assert errors[0][:4] == ["resource_type", "resource_id", "call", "severity"]
    assert [(r[1], r[3]) for r in errors[1:]] == [
        ("d2", "error"),
        ("d2", "error"),
        ("d3", "warning"),
    ]
    summary = summary_of(error.report_path)
    assert (
        summary["resources with errors"],
        summary["errors"],
        summary["warnings"],
    ) == (1, 2, 1)


def test_a_resource_whose_calls_all_failed_keeps_its_earlier_proposals(go, conn):
    go()
    before = [r for r in stored(conn) if r[0] == "d2"]
    with pytest.raises(ProposeIncompleteError):
        go(ScriptedBackend(fail_on=("Title d2",)))
    assert [r for r in stored(conn) if r[0] == "d2"] == before


def test_the_report_is_written_with_zero_errors(go, data_dir):
    summary = go(limit=2)

    assert sheet_rows(summary.report_path, "Errors") == [
        list(propose_step.ERROR_HEADER)
    ]
    totals = summary_of(summary.report_path)
    assert totals["resources processed"] == 2
    assert totals["themes stored"] == 2 and totals["keywords stored"] == 4
    assert totals["calls"] == 4 and totals["input tokens"] == 400
    assert totals["output tokens"] == 40 and totals["estimated cost USD"] > 0


def test_a_skipped_resource_is_a_warning_and_does_not_fail(go):
    summary = go()
    rows = sheet_rows(summary.report_path, "Errors")[1:]
    assert [(r[1], r[3], r[4]) for r in rows] == [
        ("d3", "warning", "skipped: no title")
    ]


def test_a_report_that_cannot_be_written_still_raises_the_failures(go, data_dir):
    (
        data_dir / "PROD" / "cat" / REPORT_NAME
    ).mkdir()  # a folder in the way: the save fails

    with pytest.raises(ProposeIncompleteError) as raised:
        go(ScriptedBackend(fail_on=("Title d2",)))

    assert raised.value.report_path is None
    assert isinstance(raised.value.report_error, OSError)
    assert "NOT written" in str(raised.value)
    assert [f.resource_id for f in raised.value.failures] == ["d2", "d2"]


def test_a_report_that_cannot_be_written_without_errors_raises_the_os_error(
    go, data_dir
):
    (data_dir / "PROD" / "cat" / REPORT_NAME).mkdir()
    with pytest.raises(OSError):
        go()


def test_an_unexpected_exception_is_an_error_of_that_resource_only(
    go, conn, monkeypatch
):
    real = propose_step.propose_keywords

    def broken(resource, client, themes, vocabulary):
        if resource.resource_id == "d2":
            raise RuntimeError("bug")
        return real(resource, client, themes, vocabulary)

    monkeypatch.setattr(propose_step, "propose_keywords", broken)

    with pytest.raises(ProposeIncompleteError) as raised:
        go()

    [failure] = raised.value.failures
    assert (failure.resource_id, failure.call) == ("d2", "step")
    assert "RuntimeError: bug" in failure.message
    # d1 is complete, and d2's themes (valid, already paid for) are saved
    assert {(r[0], r[1].endswith("#theme")) for r in stored(conn)} == {
        ("d1", True),
        ("d1", False),
        ("d2", True),
    }


# --- the dry run --------------------------------------------------------------------------------


def test_a_dry_run_calls_no_model_and_writes_nothing(go, conn, data_dir, tmp_path):
    def factory():
        raise AssertionError("a dry run must not build a backend")

    summary = run_propose(
        "PROD",
        "cat",
        conn=conn,
        fetched_data_dir=data_dir,
        dry_run=True,
        backend_factory=factory,
        log_dir=tmp_path / "logs",
    )

    assert (
        summary.dry_run and summary.calls == 4
    )  # d1 and d2, themes and keywords; d3 has no title
    assert summary.input_tokens > 0 and summary.cost_usd > 0
    assert (
        summary.warnings == 1
    )  # only the no-title one: the canned answers invent no "list cut" warning
    assert stored(conn) == []
    assert not (tmp_path / "logs").exists()
    assert not (data_dir / "PROD" / "cat" / REPORT_NAME).exists()
    assert summary.report_path is None and summary.log_path is None


# --- the pipeline wiring ------------------------------------------------------------------------


def test_propose_is_a_step_but_not_a_default_one():
    assert "propose" in pipeline.STEPS
    assert "propose" not in pipeline.DEFAULT_STEPS
    assert pipeline.parse_steps("db,propose") == ("db", "propose")
    assert pipeline.parse_steps("propose") == ("propose",)


@pytest.fixture
def key_set(monkeypatch):
    monkeypatch.setattr(pipeline, "load_api_key", lambda: "key")


def pipeline_run(tmp_path, data_dir, steps, **kwargs):
    return pipeline.run(
        "cat",
        steps=steps,
        env="PROD",
        manifests_dir=tmp_path / "manifests",
        fetched_data_dir=data_dir,
        db_path=tmp_path / "mqa.sqlite",
        **kwargs,
    )


class RecordingProposer:
    def __init__(self):
        self.calls = []

    def __call__(self, env, catalogue, **kwargs):
        self.calls.append((env, catalogue, kwargs))
        return ProposeSummary(
            env,
            catalogue,
            kwargs["dry_run"],
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0.0,
            0,
            0,
            None,
            None,
        )


def test_propose_alone_reads_the_existing_database(tmp_path, data_dir, conn, key_set):
    proposer = RecordingProposer()
    outcome = pipeline_run(
        tmp_path, data_dir, {"propose"}, limit=5, workers=2, proposer=proposer
    )
    [(env, catalogue, kwargs)] = proposer.calls
    assert (env, catalogue) == ("PROD", "cat")
    assert (kwargs["limit"], kwargs["workers"], kwargs["dry_run"]) == (5, 2, False)
    assert outcome.propose_summary is not None and outcome.load_summary is None


def test_a_run_without_the_propose_step_never_calls_the_proposer(
    tmp_path, data_dir, conn
):
    proposer = RecordingProposer()
    pipeline_run(tmp_path, data_dir, {"db"}, proposer=proposer)
    assert proposer.calls == []


def test_a_missing_key_is_an_input_error_before_anything_runs(
    tmp_path, data_dir, conn, monkeypatch
):
    def no_key():
        raise LLMInputError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(pipeline, "load_api_key", no_key)
    proposer = RecordingProposer()
    with pytest.raises(pipeline.PipelineInputError, match="ANTHROPIC_API_KEY"):
        pipeline_run(tmp_path, data_dir, {"propose"}, proposer=proposer)
    assert proposer.calls == []


def test_a_dry_run_needs_no_key(tmp_path, data_dir, conn, monkeypatch):
    def no_key():
        raise LLMInputError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(pipeline, "load_api_key", no_key)
    proposer = RecordingProposer()
    pipeline_run(tmp_path, data_dir, {"propose"}, dry_run=True, proposer=proposer)
    assert len(proposer.calls) == 1


def test_an_unknown_catalogue_is_an_input_error(tmp_path, data_dir, conn, key_set):
    with pytest.raises(pipeline.PipelineInputError, match="other"):
        pipeline.run(
            "other",
            steps={"propose"},
            env="PROD",
            fetched_data_dir=data_dir,
            db_path=tmp_path / "mqa.sqlite",
            proposer=RecordingProposer(),
        )


def test_a_missing_database_is_an_input_error_and_is_not_created(
    tmp_path, data_dir, key_set
):
    with pytest.raises(pipeline.PipelineInputError):
        pipeline_run(tmp_path, data_dir, {"propose"}, proposer=RecordingProposer())
    assert not (tmp_path / "mqa.sqlite").exists()


def test_db_then_propose_then_db_removes_the_proposals_and_logs_how_many(
    tmp_path, data_dir, key_set, caplog
):
    backend = ScriptedBackend()
    proposer = functools.partial(
        run_propose, backend_factory=lambda: backend, log_dir=tmp_path / "logs"
    )
    outcome = pipeline_run(tmp_path, data_dir, {"db", "propose"}, proposer=proposer)
    assert outcome.propose_summary.themes == 2 and outcome.propose_summary.keywords == 4
    assert outcome.propose_summary.report_path.exists()

    with caplog.at_level(logging.INFO, logger="mqa_advisor.catalogue_db"):
        reload = pipeline_run(tmp_path, data_dir, {"db"})
    assert reload.load_summary.proposals_removed == 6
    assert "removed 6 proposal(s)" in caplog.text


# --- main ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "option", [["--dry-run"], ["--limit", "3"], ["--workers", "2"]]
)
def test_propose_options_without_the_step_are_a_usage_error(
    option, monkeypatch, capsys
):
    monkeypatch.setattr(pipeline, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as raised:
        pipeline.main(["cat", *option])
    assert raised.value.code == 2
    assert "only apply to the 'propose' step" in capsys.readouterr().err


@pytest.mark.parametrize("option", [["--limit", "0"], ["--workers", "0"]])
def test_non_positive_limit_or_workers_are_a_usage_error(option, capsys):
    with pytest.raises(SystemExit) as raised:
        pipeline.main(["cat", "--steps", "propose", *option])
    assert raised.value.code == 2


def test_main_passes_the_propose_options_and_prints_the_dry_run_estimate(
    monkeypatch, capsys
):
    seen = {}

    def fake_run(catalogue, **kwargs):
        seen.update(kwargs)
        summary = ProposeSummary(
            "PROD",
            catalogue,
            True,
            3,
            0,
            0,
            0,
            0,
            0,
            7,
            700,
            0,
            70,
            0.0123,
            0,
            0,
            None,
            None,
        )
        return pipeline.PipelineOutcome("PROD", propose_summary=summary)

    monkeypatch.setattr(pipeline, "run", fake_run)
    pipeline.main(
        ["cat", "--steps", "propose", "--limit", "3", "--dry-run", "--workers", "8"]
    )

    assert (seen["limit"], seen["dry_run"], seen["workers"]) == (3, True, 8)
    out = capsys.readouterr().out
    assert "7 call(s)" in out and "ROUGH" in out and "Nothing was written" in out


def test_main_defaults_to_four_workers_and_no_limit(monkeypatch):
    seen = {}

    def fake_run(catalogue, **kwargs):
        seen.update(kwargs)
        return pipeline.PipelineOutcome("PROD")

    monkeypatch.setattr(pipeline, "run", fake_run)
    pipeline.main(["cat", "--steps", "propose"])
    assert (seen["limit"], seen["dry_run"], seen["workers"]) == (None, False, 4)


def test_main_exits_1_on_a_propose_error_with_one_stderr_line(monkeypatch, capsys):
    failures = tuple(
        propose_step.ReportRow(
            "dataset",
            f"d{i}",
            "themes",
            "error",
            "the API\nis down",
            None,
            None,
            None,
            "t",
        )
        for i in range(12)
    )

    def fake_run(catalogue, **kwargs):
        raise ProposeIncompleteError(failures, 20, pipeline.Path("report.xlsx"))

    monkeypatch.setattr(pipeline, "run", fake_run)
    with pytest.raises(SystemExit) as raised:
        pipeline.main(["cat", "--steps", "propose"])

    assert raised.value.code == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "12 error(s) in 20 resource(s)" in err and "and 2 more" in err
    assert "dataset d0 (themes: the API is down)" in err


def test_a_full_propose_run_through_main_exits_0_with_only_warnings(
    tmp_path, data_dir, conn, key_set, monkeypatch, capsys
):
    backend = ScriptedBackend()
    real_run = pipeline.run

    def wired(catalogue, **kwargs):
        return real_run(
            catalogue,
            env="PROD",
            fetched_data_dir=data_dir,
            db_path=tmp_path / "mqa.sqlite",
            proposer=functools.partial(
                run_propose, backend_factory=lambda: backend, log_dir=tmp_path / "logs"
            ),
            **kwargs,
        )

    monkeypatch.setattr(pipeline, "run", wired)
    pipeline.main(
        ["cat", "--steps", "propose"]
    )  # d3 has no title: a warning, not a failure

    out = capsys.readouterr().out
    assert "2 theme(s) and 4 keyword(s) for 2 of 3 resource(s)" in out
    assert "1 warning(s)" in out and REPORT_NAME in out
