"""Tests for the `propose` pipeline step: the run, the report, the dry run and the pipeline wiring.

No network: the model is a scripted backend and the data is a small catalogue written here and loaded with the
real loader. Nothing is written outside `tmp_path`.
"""

import functools
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import openpyxl
import pytest

from mqa_advisor import llm, pipeline, propose_step
from mqa_advisor.catalogue_db import connect, load_catalogue, read_proposals
from mqa_advisor.llm import BackendResponse, LLMCallError, LLMInputError, RunLog
from mqa_advisor.prompts import LoadedPrompt, PromptError
from mqa_advisor.propose import OWN_DATASET_PREFIX, Issue, ThemeResult
from mqa_advisor.propose_step import (
    REPORT_NAME,
    DryRunClient,
    ProposeIncompleteError,
    ProposeOptions,
    ProposeSummary,
    ReportRow,
    run_propose,
    write_report,
)
from mqa_advisor.vocabulary import VocabularyError, load_vocabulary

CONTEXT = {"dct": "http://purl.org/dc/terms/", "dcat": "http://www.w3.org/ns/dcat#"}
THEME = "http://publications.europa.eu/resource/authority/data-theme/"
AGRI = THEME + "AGRI"
ENVI = THEME + "ENVI"
SERVICE_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "ingested"
    / "nsip-lu"
    / "dataservice_https-letzdata-public-lu-data-services-1.json"
)


class ScriptedBackend:
    """Answers by prompt; fails on demand. Records every request and is safe to share between threads.

    `fail_on`: markers; a request whose user text has one fails. `fail_tool`: every request of that prompt fails.
    `slow`: marker -> seconds to wait before answering a request whose user text has it.
    """

    def __init__(
        self,
        themes=("AGRI",),
        keywords=("alpha", "beta"),
        fail_on=(),
        fail_tool=None,
        slow=None,
    ):
        self.themes = list(themes)
        self.keywords = list(keywords)
        self.fail_on = fail_on
        self.fail_tool = fail_tool
        self.slow = slow or {}
        self.requests = []
        self._lock = threading.Lock()
        self.closed = False

    def create(self, request):
        with self._lock:
            self.requests.append(request)
        for marker, seconds in self.slow.items():
            if marker in request.user:
                time.sleep(seconds)
        if request.tool_name == self.fail_tool or any(
            marker in request.user for marker in self.fail_on
        ):
            raise LLMCallError("the API is down")
        if request.tool_name == "propose_themes":
            output = {"themes": self.themes}
        else:
            output = {"keywords": self.keywords, "language": "eng"}
        return BackendResponse(output, "{}", 100, 0, 10)

    def close(self):
        self.closed = True


def _write_dataset(folder, name, title, own=True):
    base = OWN_DATASET_PREFIX if own else "https://example.org/"
    node = {"@id": base + f"dataset/{name}", "@type": "dcat:Dataset"}
    if title:
        node["dct:title"] = {"@value": title, "@language": "eng"}
    (folder / f"dataset_{name}.jsonld").write_text(
        json.dumps({"@context": CONTEXT, "@graph": [node]}), encoding="utf-8"
    )


def build_db(tmp_path, datasets=(), service=False, load_order=None):
    """A catalogue `PROD/cat` and its database. `datasets`: (name, title, own). Returns (conn, data_dir)."""
    data_dir = tmp_path / "fetched"
    folder = data_dir / "PROD" / "cat"
    folder.mkdir(parents=True)
    for name, title, own in datasets:
        _write_dataset(folder, name, title, own)
    if service:
        shutil.copy(SERVICE_FIXTURE, folder / SERVICE_FIXTURE.name)
    conn = connect(tmp_path / "mqa.sqlite")
    # `load_order`: load these resources one call at a time, so the primary keys do not follow the ids
    for rid in load_order or [None]:
        load_catalogue(
            "PROD",
            "cat",
            conn=conn,
            data_dir=data_dir,
            resource_ids=None if rid is None else [rid],
        )
    return conn, data_dir


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


def propose(conn, data_dir, tmp_path, backend=None, **options):
    backend = backend or ScriptedBackend()
    return run_propose(
        "PROD",
        "cat",
        conn=conn,
        fetched_data_dir=data_dir,
        options=ProposeOptions(**options),
        backend_factory=lambda: backend,
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def go(tmp_path, data_dir, conn):
    return functools.partial(propose, conn, data_dir, tmp_path)


def pk_of(conn, resource_id):
    return conn.execute(
        "SELECT resource_pk FROM resource WHERE resource_id = ?", (resource_id,)
    ).fetchone()[0]


def stored(conn):
    """The stored proposals as comparable tuples (without the timestamp), ordered."""
    return conn.execute(
        "SELECT r.resource_id, p.property_uri, p.value, p.value_kind, p.lang, p.prompt_name, p.model"
        " FROM proposed_property_value p JOIN resource r USING (resource_pk)"
        " ORDER BY r.resource_id, p.property_uri, p.rowid"
    ).fetchall()


def values_of(conn, resource_id, kind):
    """The stored values of one property ('theme' or 'keyword') of a resource."""
    return [
        r[2] for r in stored(conn) if r[0] == resource_id and r[1].endswith("#" + kind)
    ]


def sheet_rows(path, name):
    workbook = openpyxl.load_workbook(path, read_only=True)
    try:
        return [list(row) for row in workbook[name].iter_rows(values_only=True)]
    finally:
        workbook.close()


def summary_of(path):
    return {row[0]: row[1] for row in sheet_rows(path, "Summary")[1:]}


# --- the run ------------------------------------------------------------------------------------


def test_stores_themes_and_keywords_and_warns_about_a_resource_without_title(go, conn):
    summary = go()

    rows = stored(conn)
    assert {r[0] for r in rows} == {"d1", "d2"}  # d3 has no title: nothing stored
    assert values_of(conn, "d1", "theme") == [AGRI]
    assert values_of(conn, "d2", "theme") == [AGRI]
    assert values_of(conn, "d1", "keyword") == ["alpha", "beta"]
    assert values_of(conn, "d2", "keyword") == ["alpha", "beta"]
    assert {(r[3], r[4]) for r in rows if r[1].endswith("#theme")} == {("uri", None)}
    assert {(r[3], r[4]) for r in rows if r[1].endswith("#keyword")} == {
        ("literal", "eng")
    }
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
    )  # the "no title" one, listed once


def test_a_second_run_replaces_the_proposals(go, conn):
    go()
    go(ScriptedBackend(themes=("ENVI",), keywords=("gamma",)))
    assert values_of(conn, "d1", "theme") == [ENVI]
    assert values_of(conn, "d1", "keyword") == ["gamma"]
    assert len(stored(conn)) == 4  # nothing was appended


def test_limit_takes_the_first_resources_by_id(go, conn):
    summary = go(limit=1)
    assert summary.resources == 1
    assert {r[0] for r in stored(conn)} == {"d1"}


def test_limit_follows_the_resource_id_not_the_insertion_order(tmp_path):
    # `zz` is loaded first, so it has the lower primary key; `aa` must still be the first by id.
    conn, data_dir = build_db(
        tmp_path,
        [("zz", "Title zz", True), ("aa", "Title aa", True)],
        load_order=["zz", "aa"],
    )
    propose(conn, data_dir, tmp_path, limit=1)
    assert {r[0] for r in stored(conn)} == {"aa"}
    conn.close()


def test_results_are_identical_for_one_and_four_workers(go, conn):
    go(workers=1)
    with_one = stored(conn)
    conn.execute("DELETE FROM proposed_property_value")
    conn.commit()
    go(workers=4)
    assert stored(conn) == with_one


def test_writes_are_in_resource_order_from_the_main_thread(go, conn, monkeypatch):
    calls = []
    real = propose_step.replace_proposals

    def spy(connection, resource_pk, values):
        calls.append(
            (resource_pk, threading.current_thread() is threading.main_thread())
        )
        return real(connection, resource_pk, values)

    monkeypatch.setattr(propose_step, "replace_proposals", spy)
    # d1 answers last (the slowest), yet it must still be written first
    go(ScriptedBackend(slow={"Title d1": 0.3}), workers=4)

    assert [pk for pk, _ in calls] == [pk_of(conn, r) for r in ("d1", "d2", "d3")]
    assert all(main for _, main in calls)


def test_options_must_be_positive():
    with pytest.raises(ValueError):
        ProposeOptions(workers=0)
    with pytest.raises(ValueError):
        ProposeOptions(limit=0)
    assert ProposeOptions().workers == 4


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


def test_a_standalone_data_service_is_proposed_for(tmp_path):
    conn, data_dir = build_db(tmp_path, service=True)
    summary = propose(conn, data_dir, tmp_path)
    kinds = {
        r[0] for r in conn.execute("SELECT entity_kind FROM proposed_property_value")
    }
    assert summary.resources == 1 and summary.stored == 1
    assert kinds == {"dataservice"}
    conn.close()


def test_a_resource_without_an_own_entity_is_skipped_with_a_warning(tmp_path):
    conn, data_dir = build_db(
        tmp_path, [("x1", "Title x1", False), ("x2", "Title x2", True)]
    )
    summary = propose(conn, data_dir, tmp_path)
    assert {r[0] for r in stored(conn)} == {"x2"}
    assert (summary.skipped, summary.failed, summary.errors) == (1, 0, 0)
    assert summary.warnings == 1
    conn.close()


# --- failures and the report --------------------------------------------------------------------


def run_failing(go, backend):
    with pytest.raises(ProposeIncompleteError) as raised:
        go(backend)
    return raised.value


def test_one_failing_resource_saves_the_others_reports_and_raises(go, conn, data_dir):
    error = run_failing(go, ScriptedBackend(fail_on=("Title d2",)))

    assert {r[0] for r in stored(conn)} == {"d1"}
    assert [(f.resource_id, f.call, f.severity) for f in error.failures] == [
        ("d2", "themes", "error"),
        ("d2", "keywords", "error"),
    ]
    assert error.report_path == data_dir / "PROD" / "cat" / REPORT_NAME
    summary = summary_of(error.report_path)
    assert summary["resources with errors"] == 1
    assert (summary["errors"], summary["warnings"]) == (2, 1)
    assert (error.summary.failed, error.summary.stored) == (1, 1)


def test_every_column_of_the_error_rows_is_filled(go):
    error = run_failing(go, ScriptedBackend(fail_on=("Title d2",)))

    errors = sheet_rows(error.report_path, "Errors")
    assert errors[0] == list(propose_step.ERROR_HEADER)
    by_call = {r[2]: r for r in errors[1:] if r[1] == "d2"}
    stamp = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
    themes = by_call["themes"]
    assert themes[:4] == ["dataset", "d2", "themes", "error"]
    assert "theme call failed" in themes[4] and "the API is down" in themes[4]
    assert (themes[5], themes[6], themes[7]) == (
        "propose_themes",
        1,
        "claude-haiku-5-5",
    )
    assert stamp.fullmatch(themes[8])
    assert by_call["keywords"][5] == "propose_keywords"
    warning = [r for r in errors[1:] if r[1] == "d3"][
        0
    ]  # a skip: no call, so no prompt
    assert warning[:5] == ["dataset", "d3", "themes", "warning", "skipped: no title"]
    assert warning[5:8] == [None, None, None] and stamp.fullmatch(warning[8])


def test_errors_are_listed_before_warnings_of_the_same_resource(tmp_path):
    rows = [
        ReportRow("dataset", "d1", "themes", "warning", "w", None, None, None, "t"),
        ReportRow("dataset", "d1", "keywords", "error", "e", None, None, None, "t"),
    ]
    summary = ProposeSummary(
        "PROD", "cat", False, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0.0, 1, 1, None, None
    )
    path = tmp_path / REPORT_NAME
    write_report(path, summary, rows, RunLog("cat", enabled=False))
    assert [r[3] for r in sheet_rows(path, "Errors")[1:]] == ["error", "warning"]


def test_the_whole_summary_sheet_of_a_clean_run(go):
    summary = go(limit=2)
    totals = summary_of(summary.report_path)

    assert sheet_rows(summary.report_path, "Errors") == [
        list(propose_step.ERROR_HEADER)
    ]
    assert re.fullmatch(r"[0-9a-f]{8}", totals["run id"])
    assert totals["log file"].endswith(".log") and "logs" in totals["log file"]
    assert totals["prices as of"] == llm.PRICES_AS_OF
    expected = {
        "environment": "PROD",
        "catalogue": "cat",
        "resources processed": 2,
        "resources with proposals": 2,
        "resources skipped": 0,
        "resources with errors": 0,
        "themes stored": 2,
        "keywords stored": 4,
        "errors": 0,
        "warnings": 0,
        "calls": 4,
        "input tokens": 400,
        "cached input tokens": 0,
        "output tokens": 40,
    }
    assert {k: totals[k] for k in expected} == expected
    assert totals["estimated cost USD"] > 0 and "answered calls" in totals["note"]


def test_a_skipped_resource_is_a_warning_and_does_not_fail(go):
    summary = go()
    rows = sheet_rows(summary.report_path, "Errors")[1:]
    assert [(r[1], r[3], r[4]) for r in rows] == [
        ("d3", "warning", "skipped: no title")
    ]


def test_a_failed_call_keeps_the_earlier_proposals_of_its_property(go, conn):
    go()  # themes AGRI, keywords alpha and beta
    # the themes call succeeds with a new answer, the keywords call fails: new themes, old keywords
    with pytest.raises(ProposeIncompleteError):
        go(ScriptedBackend(themes=("ENVI",), fail_tool="propose_keywords"))
    assert values_of(conn, "d1", "theme") == [ENVI]
    assert values_of(conn, "d1", "keyword") == ["alpha", "beta"]

    # and the other way round: old themes kept, keywords replaced
    with pytest.raises(ProposeIncompleteError):
        go(ScriptedBackend(keywords=("gamma",), fail_tool="propose_themes"))
    assert values_of(conn, "d1", "theme") == [ENVI]
    assert values_of(conn, "d1", "keyword") == ["gamma"]


def test_a_resource_whose_calls_all_failed_keeps_its_earlier_proposals(go, conn):
    go()
    before = [r for r in stored(conn) if r[0] == "d2"]
    run_failing(go, ScriptedBackend(fail_on=("Title d2",)))
    assert [r for r in stored(conn) if r[0] == "d2"] == before
    assert read_proposals(conn, pk_of(conn, "d2"))  # still there, not rewritten empty


def test_an_unreadable_resource_is_an_error_of_that_resource_only(
    go, conn, monkeypatch
):
    real = propose_step.read_resource
    bad = pk_of(conn, "d2")

    def read(connection, pk):
        if pk == bad:
            raise ValueError("a theme node without an id")
        return real(connection, pk)

    monkeypatch.setattr(propose_step, "read_resource", read)
    error = run_failing(go, None)

    assert {r[0] for r in stored(conn)} == {"d1"}
    [failure] = error.failures
    assert (failure.resource_id, failure.call) == ("d2", "step")
    assert "could not read the resource: ValueError: a theme node" in failure.message
    assert error.report_path.exists()


def test_a_resource_that_cannot_be_stored_is_an_error_and_the_others_are_stored(
    go, conn, monkeypatch
):
    real = propose_step.replace_proposals
    bad = pk_of(conn, "d2")

    def store(connection, pk, values):
        if pk == bad:
            raise sqlite3.OperationalError("database is locked")
        return real(connection, pk, values)

    monkeypatch.setattr(propose_step, "replace_proposals", store)
    error = run_failing(go, None)

    assert {r[0] for r in stored(conn)} == {"d1"}
    [failure] = error.failures
    assert failure.resource_id == "d2"
    assert "could not be stored" in failure.message and "locked" in failure.message
    assert error.summary.failed == 1 and error.summary.stored == 1
    assert (
        error.summary.themes == 1
    )  # d2's themes were not stored, so they are not counted
    assert error.report_path.exists()


def test_a_successful_call_that_cannot_be_stored_is_an_error_not_a_silent_drop(
    go, conn, monkeypatch
):
    def themes_without_prompt(resource, client, vocabulary):
        return ThemeResult(resource.resource_id, "ok", (AGRI,), (), None, 1, ())

    monkeypatch.setattr(propose_step, "propose_themes", themes_without_prompt)
    error = run_failing(go, None)

    assert {f.resource_id for f in error.failures} == {
        "d1",
        "d2",
        "d3",
    }  # the fake says "ok" for all
    assert "cannot be stored" in error.failures[0].message
    assert stored(conn) == []


def test_a_failed_call_without_an_error_issue_still_gets_an_error_row(go, monkeypatch):
    def failed(resource, client, vocabulary):
        issue = Issue("warning", resource.resource_id, "something odd")
        return ThemeResult(resource.resource_id, "error", (), (), None, 1, (issue,))

    monkeypatch.setattr(propose_step, "propose_themes", failed)
    error = run_failing(go, None)

    assert {(f.resource_id, f.call) for f in error.failures} == {
        ("d1", "themes"),
        ("d2", "themes"),
        ("d3", "themes"),  # the fake fails every resource
    }
    assert "failed without giving a reason" in error.failures[0].message


def test_a_report_that_cannot_be_written_still_raises_the_failures(go, data_dir):
    (
        data_dir / "PROD" / "cat" / REPORT_NAME
    ).mkdir()  # a folder in the way: the save fails

    error = run_failing(go, ScriptedBackend(fail_on=("Title d2",)))

    assert error.report_path is None
    assert isinstance(error.report_error, OSError)
    assert "NOT written" in str(error)
    assert [f.resource_id for f in error.failures] == ["d2", "d2"]


def test_a_report_that_cannot_be_written_without_errors_raises_the_os_error(
    go, data_dir
):
    (data_dir / "PROD" / "cat" / REPORT_NAME).mkdir()
    with pytest.raises(OSError):
        go()


def test_any_exception_of_the_report_write_is_reported_with_the_failures(
    go, monkeypatch
):
    def broken_save(self, filename):
        raise TypeError("a value the library rejects")

    monkeypatch.setattr(openpyxl.Workbook, "save", broken_save)

    error = run_failing(go, ScriptedBackend(fail_on=("Title d2",)))
    assert isinstance(error.report_error, TypeError)
    assert error.report_path is None and len(error.failures) == 2

    with pytest.raises(
        TypeError
    ):  # without failures the report's own exception is raised
        go()


def test_a_failed_report_write_leaves_the_previous_report_and_no_temporary_file(
    go, data_dir, monkeypatch
):
    report = data_dir / "PROD" / "cat" / REPORT_NAME
    go()
    before = report.read_bytes()

    def half_written(self, filename):
        Path(filename).write_bytes(b"garbage")
        raise OSError("disk full")

    monkeypatch.setattr(openpyxl.Workbook, "save", half_written)
    with pytest.raises(OSError):
        go()

    assert report.read_bytes() == before
    assert list(report.parent.glob("*.tmp")) == []


def test_report_cells_lose_illegal_characters_and_are_never_formulas(tmp_path):
    rows = [
        ReportRow(
            "dataset",
            "=cmd|x",
            "themes",
            "error",
            "bell\x07 =1+1",
            None,
            None,
            None,
            "t",
        ),
        ReportRow("dataset", "d2", "themes", "error", "=1+1", None, None, None, "t"),
    ]
    summary = ProposeSummary(
        "PROD", "cat", False, 2, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0.0, 2, 0, None, None
    )
    path = tmp_path / REPORT_NAME
    write_report(path, summary, rows, RunLog("cat", enabled=False))

    sheet = openpyxl.load_workbook(path)["Errors"]
    cells = {c.coordinate: c for row in sheet.iter_rows(min_row=2) for c in row}
    assert cells["E2"].value == "bell =1+1"  # the control character is gone
    assert cells["B2"].value == "=cmd|x" and cells["B2"].data_type == "s"
    assert cells["E3"].value == "=1+1" and cells["E3"].data_type == "s"


def test_an_unexpected_exception_is_an_error_of_that_resource_only(
    go, conn, monkeypatch
):
    real = propose_step.propose_keywords

    def broken(resource, client, themes, vocabulary):
        if resource.resource_id == "d2":
            raise RuntimeError("bug")
        return real(resource, client, themes, vocabulary)

    monkeypatch.setattr(propose_step, "propose_keywords", broken)
    error = run_failing(go, None)

    [failure] = error.failures
    assert (failure.resource_id, failure.call) == ("d2", "step")
    assert "RuntimeError: bug" in failure.message
    # d1 is complete, and d2's themes (valid, already paid for) are saved
    assert {(r[0], r[1].endswith("#theme")) for r in stored(conn)} == {
        ("d1", True),
        ("d1", False),
        ("d2", True),
    }


def test_a_broken_prompt_stops_the_run_and_closes_the_backend(go, conn, monkeypatch):
    def broken(resource, client, vocabulary):
        raise PromptError("propose_themes v1: max_items is 3")

    monkeypatch.setattr(propose_step, "propose_themes", broken)
    backend = ScriptedBackend()
    with pytest.raises(PromptError):
        go(backend)

    assert backend.closed and stored(conn) == []


def test_a_backend_factory_that_raises_stops_the_run_before_any_write(
    conn, data_dir, tmp_path
):
    def factory():
        raise LLMInputError("no key")

    with pytest.raises(LLMInputError):
        run_propose(
            "PROD",
            "cat",
            conn=conn,
            fetched_data_dir=data_dir,
            backend_factory=factory,
            log_dir=tmp_path / "logs",
        )
    assert stored(conn) == []
    assert not (data_dir / "PROD" / "cat" / REPORT_NAME).exists()


def test_a_run_log_that_cannot_be_written_is_raised_and_named_in_the_failure(
    go, monkeypatch
):
    problem = llm.LLMError("writing the run log failed")
    monkeypatch.setattr(propose_step, "_close_log", lambda run_log: problem)

    with pytest.raises(llm.LLMError, match="run log"):
        go()  # no failures: the log problem itself is raised
    error = run_failing(go, ScriptedBackend(fail_on=("Title d2",)))
    assert error.log_error is problem and "run log is incomplete" in str(error)


# --- the dry run --------------------------------------------------------------------------------


def dry_run(conn, data_dir, tmp_path, **kwargs):
    def factory():
        raise AssertionError("a dry run must not build a backend")

    return run_propose(
        "PROD",
        "cat",
        conn=conn,
        fetched_data_dir=data_dir,
        options=ProposeOptions(dry_run=True, **kwargs),
        backend_factory=factory,
        log_dir=tmp_path / "logs",
    )


def test_a_dry_run_calls_no_model_and_writes_nothing(conn, data_dir, tmp_path):
    summary = dry_run(conn, data_dir, tmp_path)

    assert (
        summary.dry_run and summary.calls == 4
    )  # d1 and d2, themes and keywords; d3 has no title
    assert summary.input_tokens > 0 and summary.cost_usd > 0
    assert (
        summary.warnings == 1
    )  # only the no-title one: the canned answers invent no "list cut"
    assert (summary.failed, summary.errors) == (0, 0)
    assert stored(conn) == []
    assert not (tmp_path / "logs").exists()
    assert not (data_dir / "PROD" / "cat" / REPORT_NAME).exists()
    assert summary.report_path is None and summary.log_path is None


def test_a_dry_run_counts_the_calls_of_a_clean_run(go, conn, data_dir, tmp_path):
    estimate = dry_run(conn, data_dir, tmp_path)
    real = go()
    assert estimate.calls == real.calls


def test_a_dry_run_leaves_an_existing_report_untouched(conn, data_dir, tmp_path):
    report = data_dir / "PROD" / "cat" / REPORT_NAME
    report.write_bytes(b"previous report")
    dry_run(conn, data_dir, tmp_path)
    assert report.read_bytes() == b"previous report"


def test_a_dry_run_that_meets_an_error_fails_without_a_report(
    conn, data_dir, tmp_path, monkeypatch
):
    def broken(resource, client, themes, vocabulary):
        raise RuntimeError("cannot build the prompt")

    monkeypatch.setattr(propose_step, "propose_keywords", broken)
    with pytest.raises(ProposeIncompleteError) as raised:
        dry_run(conn, data_dir, tmp_path)

    assert "dry run, nothing written" in str(raised.value)
    assert raised.value.summary.dry_run and raised.value.report_path is None
    assert not (data_dir / "PROD" / "cat" / REPORT_NAME).exists()


def test_a_dry_run_counts_an_unpriced_model_as_zero_without_failing(
    conn, data_dir, tmp_path, monkeypatch, caplog
):
    def unpriced(model, *tokens):
        raise LLMInputError(f"no price for {model}")

    monkeypatch.setattr(propose_step, "estimate_cost", unpriced)
    with caplog.at_level(logging.WARNING, logger="mqa_advisor.propose_step"):
        summary = dry_run(conn, data_dir, tmp_path)

    assert summary.failed == 0 and summary.cost_usd == 0.0 and summary.calls == 4
    assert caplog.text.count("is not priced") == 1  # once per model, not per call


def prompt(name, user):
    return LoadedPrompt(name, 1, "claude-haiku-5-5", 100, 5, "system", user, "sha")


def test_the_dry_run_answer_has_the_number_of_themes_asked_for_and_no_existing_one():
    client = DryRunClient(load_vocabulary())
    user = "Existing:\nTRAN — Transport\n\nReturn up to 2 additional themes."
    themes = client.call(prompt("propose_themes", user), resource="r").output["themes"]
    assert len(themes) == 2 and "TRAN" not in themes


def test_the_dry_run_fails_loudly_when_the_prompt_no_longer_says_how_many():
    client = DryRunClient(load_vocabulary())
    with pytest.raises(ValueError, match="up to N"):
        client.call(prompt("propose_themes", "Give me some themes."), resource="r")


# --- the pipeline wiring ------------------------------------------------------------------------


def test_propose_is_a_step_but_not_a_default_one():
    assert "propose" in pipeline.STEPS
    assert "propose" not in pipeline.DEFAULT_STEPS
    assert pipeline.parse_steps("db,propose") == ("db", "propose")
    assert pipeline.parse_steps("propose") == ("propose",)


@pytest.fixture
def key_set(monkeypatch):
    monkeypatch.setattr(pipeline, "load_api_key", lambda: "key")


def no_key(monkeypatch):
    def missing():
        raise LLMInputError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(pipeline, "load_api_key", missing)


def pipeline_run(tmp_path, data_dir, steps=None, env="PROD", **kwargs):
    if steps is not None:
        kwargs["steps"] = steps
    return pipeline.run(
        "cat",
        env=env,
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
        return summary_with(dry_run=kwargs["options"].dry_run)


def test_propose_alone_reads_the_existing_database(tmp_path, data_dir, conn, key_set):
    proposer = RecordingProposer()
    outcome = pipeline_run(
        tmp_path, data_dir, {"propose"}, limit=5, workers=2, proposer=proposer
    )
    [(env, catalogue, kwargs)] = proposer.calls
    assert (env, catalogue) == ("PROD", "cat")
    assert kwargs["options"] == ProposeOptions(limit=5, dry_run=False, workers=2)
    assert outcome.propose_summary is not None and outcome.load_summary is None


def test_a_run_without_the_propose_step_never_calls_the_proposer(
    tmp_path, data_dir, conn
):
    proposer = RecordingProposer()
    pipeline_run(tmp_path, data_dir, {"db"}, proposer=proposer)
    assert proposer.calls == []


def test_the_default_steps_do_not_propose(tmp_path, data_dir, conn):
    proposer = RecordingProposer()
    ran = []

    class Ingestor:
        def run_catalogue(self, catalogue, force=False):
            ran.append("fetch")

        def shutdown(self):
            pass

    pipeline_run(
        tmp_path,
        data_dir,
        session_factory=lambda env: "session",
        manifest_generator=lambda *a, **k: ran.append("manifest") or Path("m.xlsx"),
        ingestor_factory=lambda *a, **k: Ingestor(),
        loader=lambda *a, **k: ran.append("db"),
        proposer=proposer,
    )
    assert ran == ["manifest", "fetch", "db"] and proposer.calls == []


def test_main_without_steps_runs_the_default_steps(monkeypatch):
    seen = {}

    def fake_run(catalogue, **kwargs):
        seen.update(kwargs)
        return pipeline.PipelineOutcome("PROD")

    monkeypatch.setattr(pipeline, "run", fake_run)
    pipeline.main(["cat"])
    assert seen["steps"] == pipeline.DEFAULT_STEPS


def test_a_missing_key_is_an_input_error_before_anything_runs(
    tmp_path, data_dir, conn, monkeypatch
):
    no_key(monkeypatch)
    proposer = RecordingProposer()
    with pytest.raises(pipeline.PipelineInputError, match="ANTHROPIC_API_KEY"):
        pipeline_run(tmp_path, data_dir, {"propose"}, proposer=proposer)
    assert proposer.calls == []
    assert not (tmp_path / "logs").exists()


def test_the_checks_come_before_the_db_step(tmp_path, data_dir, monkeypatch):
    no_key(monkeypatch)
    loaded = []
    with pytest.raises(pipeline.PipelineInputError, match="ANTHROPIC_API_KEY"):
        pipeline_run(
            tmp_path,
            data_dir,
            {"db", "propose"},
            loader=lambda *a, **k: loaded.append(1),
            proposer=RecordingProposer(),
        )
    assert loaded == [] and not (tmp_path / "mqa.sqlite").exists()


def test_a_dry_run_needs_no_key(tmp_path, data_dir, conn, monkeypatch):
    no_key(monkeypatch)
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


def test_a_catalogue_loaded_for_another_environment_does_not_count(
    tmp_path, data_dir, conn, key_set
):
    with pytest.raises(pipeline.PipelineInputError):
        pipeline_run(
            tmp_path, data_dir, {"propose"}, env="PPE", proposer=RecordingProposer()
        )


def test_a_missing_database_is_an_input_error_and_is_not_created(
    tmp_path, data_dir, key_set
):
    with pytest.raises(pipeline.PipelineInputError):
        pipeline_run(tmp_path, data_dir, {"propose"}, proposer=RecordingProposer())
    assert not (tmp_path / "mqa.sqlite").exists()


def test_checking_the_database_never_changes_the_file(tmp_path, data_dir, key_set):
    path = tmp_path / "mqa.sqlite"
    sqlite3.connect(path).close()  # a database that was never loaded: no tables at all
    before = path.read_bytes()

    assert pipeline._catalogue_in_database(path, "PROD", "cat") is False
    with pytest.raises(pipeline.PipelineInputError):
        pipeline_run(tmp_path, data_dir, {"propose"}, proposer=RecordingProposer())
    assert path.read_bytes() == before


def test_a_catalogue_that_is_empty_after_the_load_is_an_input_error(tmp_path, key_set):
    folder = tmp_path / "fetched" / "PROD" / "cat"
    folder.mkdir(
        parents=True
    )  # a folder with no resource files: the load leaves no rows
    proposer = RecordingProposer()
    with pytest.raises(pipeline.PipelineInputError, match="no resources"):
        pipeline_run(
            tmp_path, tmp_path / "fetched", {"db", "propose"}, proposer=proposer
        )
    assert proposer.calls == []


def test_an_unusable_vocabulary_is_an_input_error_before_anything_runs(
    tmp_path, data_dir, conn, key_set, monkeypatch
):
    def broken():
        raise VocabularyError("the file is malformed")

    monkeypatch.setattr(pipeline, "load_vocabulary", broken)
    with pytest.raises(pipeline.PipelineInputError, match="vocabulary"):
        pipeline_run(tmp_path, data_dir, {"propose"}, proposer=RecordingProposer())


def test_a_missing_prompt_file_is_an_input_error(
    tmp_path, data_dir, conn, key_set, monkeypatch
):
    monkeypatch.setattr(pipeline, "PROMPTS_DIR", tmp_path / "no-prompts")
    with pytest.raises(pipeline.PipelineInputError, match="prompt file"):
        pipeline_run(tmp_path, data_dir, {"propose"}, proposer=RecordingProposer())


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
    assert "must be positive" in capsys.readouterr().err


def summary_with(**changes):
    values = {
        "env": "PROD",
        "catalogue": "cat",
        "dry_run": False,
        "resources": 20,
        "stored": 5,
        "skipped": 1,
        "failed": 14,
        "themes": 7,
        "keywords": 9,
        "calls": 40,
        "input_tokens": 4000,
        "cached_tokens": 0,
        "output_tokens": 400,
        "cost_usd": 0.0123,
        "errors": 14,
        "warnings": 2,
        "report_path": Path("report.xlsx"),
        "log_path": None,
    }
    values.update(changes)
    return ProposeSummary(**values)


def test_main_passes_the_propose_options_and_prints_the_dry_run_estimate(
    monkeypatch, capsys
):
    seen = {}

    def fake_run(catalogue, **kwargs):
        seen.update(kwargs)
        summary = summary_with(dry_run=True, resources=3, calls=7, input_tokens=700)
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


def test_main_exits_1_on_a_propose_error_with_the_summary_and_one_stderr_line(
    monkeypatch, capsys
):
    failures = tuple(
        ReportRow(
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
        raise ProposeIncompleteError(failures, summary_with(), Path("report.xlsx"))

    monkeypatch.setattr(pipeline, "run", fake_run)
    with pytest.raises(SystemExit) as raised:
        pipeline.main(["cat", "--steps", "propose"])

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1
    assert (
        "12 error(s) in 20 resource(s)" in captured.err and "and 2 more" in captured.err
    )
    assert "dataset d0 (themes: the API is down)" in captured.err
    assert "7 theme(s) and 9 keyword(s) for 5 of 20 resource(s)" in captured.out
    assert "14 error(s)" in captured.out


@pytest.mark.parametrize(
    "error",
    [
        VocabularyError("malformed"),
        RuntimeError("a database with an old layout"),
        llm.LLMError("the run log is incomplete"),
        PromptError("a broken prompt"),
    ],
)
def test_main_exits_1_with_a_message_on_setup_errors(error, monkeypatch, capsys):
    def fake_run(catalogue, **kwargs):
        raise error

    monkeypatch.setattr(pipeline, "run", fake_run)
    with pytest.raises(SystemExit) as raised:
        pipeline.main(["cat", "--steps", "propose"])

    assert raised.value.code == 1
    assert type(error).__name__ in capsys.readouterr().err


def test_main_exits_1_on_a_missing_key(tmp_path, data_dir, conn, monkeypatch, capsys):
    no_key(monkeypatch)
    real_run = pipeline.run

    def wired(catalogue, **kwargs):
        return real_run(
            catalogue,
            env="PROD",
            fetched_data_dir=data_dir,
            db_path=tmp_path / "mqa.sqlite",
            **kwargs,
        )

    monkeypatch.setattr(pipeline, "run", wired)
    with pytest.raises(SystemExit) as raised:
        pipeline.main(["cat", "--steps", "propose"])
    assert raised.value.code == 1 and "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_main_shows_the_application_logs_but_not_third_party_info(monkeypatch):
    app = logging.getLogger("mqa_advisor")
    monkeypatch.setattr(app, "level", app.level)  # restored after the test
    monkeypatch.setattr(
        pipeline, "run", lambda c, **k: pipeline.PipelineOutcome("PROD")
    )

    pipeline.main(["cat"])

    assert app.level == logging.INFO
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


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
