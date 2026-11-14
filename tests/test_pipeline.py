"""Tests for the manifest -> fetch -> db pipeline with its step selector (no network, no DB)."""

from pathlib import Path

import pytest

from mqa_advisor import pipeline
from mqa_advisor.catalogue_db import LoadSummary
from mqa_advisor.ingest import (
    MAX_FETCH_ATTEMPTS,
    FetchFailure,
    FetchIncompleteError,
    ResourceFetchError,
)
from mqa_advisor.pipeline import PipelineInputError, parse_steps, run

SUMMARY = LoadSummary(resources=2, pruned=0, rows=10, orphan_nodes=0)


class Recorder:
    """Fakes for the four collaborators of `run`, recording the calls in order."""

    def __init__(self, tmp_path: Path):
        self.calls: list[tuple] = []
        self.tmp_path = tmp_path

    def session_factory(self, env):
        self.calls.append(("session", env))
        return "session"

    def manifest_generator(self, catalogue, *, env, session, manifests_dir):
        self.calls.append(("manifest", catalogue, env, session))
        return manifests_dir / f"{catalogue}.xlsx"

    def ingestor_factory(self, session, *, manifests_dir, output_root, env):
        self.calls.append(("ingestor", session, env, output_root))
        recorder = self

        class Ingestor:
            def run_catalogue(self, catalogue, force=False):
                recorder.calls.append(("fetch", catalogue, force))
                return output_root / env / catalogue / "report.xlsx"

            def shutdown(self):
                recorder.calls.append(("shutdown",))

        return Ingestor()

    def loader(self, env, catalogue, *, conn, data_dir):
        self.calls.append(("db", env, catalogue, data_dir))
        return SUMMARY

    def run(self, catalogue="cat", **kwargs):
        paths = {
            "manifests_dir": self.tmp_path / "manifests",
            "fetched_data_dir": self.tmp_path / "fetched",
            "db_path": self.tmp_path / "fetched" / "mqa.sqlite",
        }
        (self.tmp_path / "fetched").mkdir(exist_ok=True)
        options = {
            "env": "PROD",
            "session_factory": self.session_factory,
            "manifest_generator": self.manifest_generator,
            "ingestor_factory": self.ingestor_factory,
            "loader": self.loader,
            **paths,
            **kwargs,
        }
        return run(catalogue, **options)

    def names(self):
        return [call[0] for call in self.calls]


@pytest.fixture
def rec(tmp_path):
    return Recorder(tmp_path)


def test_all_steps_run_in_order_and_share_one_session(rec):
    outcome = rec.run()

    assert rec.names() == ["session", "manifest", "ingestor", "fetch", "shutdown", "db"]
    assert [c for c in rec.calls if c[0] == "session"] == [("session", "PROD")]
    assert outcome.manifest_path.name == "cat.xlsx"
    assert outcome.fetch_report_path.name == "report.xlsx"
    assert outcome.load_summary == SUMMARY


def test_manifest_only_runs_just_the_manifest(rec):
    outcome = rec.run(steps={"manifest"})

    assert rec.names() == ["session", "manifest"]
    assert outcome.fetch_report_path is None
    assert outcome.load_summary is None


def test_db_only_needs_no_network_and_reads_the_existing_files(rec, tmp_path):
    (tmp_path / "fetched" / "PROD" / "cat").mkdir(parents=True)

    outcome = rec.run(steps={"db"})

    assert rec.names() == ["db"]  # no session built, nothing fetched
    assert outcome.manifest_path is None
    assert outcome.load_summary == SUMMARY


def test_fetch_and_db_reuse_the_existing_manifest(rec, tmp_path):
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "cat.xlsx").touch()

    rec.run(steps={"fetch", "db"})

    assert rec.names() == ["session", "ingestor", "fetch", "shutdown", "db"]


def test_db_without_fetched_files_fails_before_anything_runs(rec):
    with pytest.raises(PipelineInputError, match="fetch"):
        rec.run(steps={"db"})

    assert rec.calls == []


def test_fetch_without_a_manifest_fails_before_anything_runs(rec):
    with pytest.raises(PipelineInputError, match="manifest"):
        rec.run(steps={"fetch", "db"})

    assert rec.calls == []


def test_force_is_passed_to_the_fetch(rec):
    rec.run(steps={"manifest", "fetch"}, force=True)

    assert ("fetch", "cat", True) in rec.calls


def test_ingestor_is_shut_down_when_the_fetch_fails(rec):
    def failing_ingestor(session, **kwargs):
        ingestor = rec.ingestor_factory(session, **kwargs)

        def boom(catalogue, force=False):
            raise RuntimeError("fetch failed")

        ingestor.run_catalogue = boom
        return ingestor

    with pytest.raises(RuntimeError, match="fetch failed"):
        rec.run(steps={"manifest", "fetch"}, ingestor_factory=failing_ingestor)

    assert rec.names()[-1] == "shutdown"


def _incomplete_error(n_failures=1):
    failures = [
        FetchFailure("dataset", f"id-{i}", RuntimeError(f"boom-{i}"))
        for i in range(n_failures)
    ]
    return FetchIncompleteError(Path("out/report.xlsx"), failures, total=20)


def test_incomplete_fetch_skips_the_db_step_and_shuts_the_ingestor_down(rec):
    def incomplete_ingestor(session, **kwargs):
        ingestor = rec.ingestor_factory(session, **kwargs)

        def incomplete(catalogue, force=False):
            raise _incomplete_error()

        ingestor.run_catalogue = incomplete
        return ingestor

    with pytest.raises(FetchIncompleteError):
        rec.run(ingestor_factory=incomplete_ingestor)

    assert "db" not in rec.names()
    assert rec.names()[-1] == "shutdown"


def test_main_exits_1_with_counts_and_report_path_on_incomplete_fetch(
    monkeypatch, capsys
):
    def fake_run(catalogue, *, steps, force):
        raise _incomplete_error(n_failures=12)

    monkeypatch.setattr(pipeline, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        pipeline.main(["nsip-lu"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("FetchIncompleteError: 12 of 20 resources failed")
    assert str(Path("out/report.xlsx")) in err
    assert "dataset id-0 (RuntimeError: boom-0)" in err
    assert "id-9" in err
    assert "id-10" not in err  # only the first 10 failures are listed


def test_a_failing_load_is_fatal(rec, tmp_path):
    (tmp_path / "fetched" / "PROD" / "cat").mkdir(parents=True)

    def failing_loader(*args, **kwargs):
        raise ValueError("bad file")

    with pytest.raises(ValueError, match="bad file"):
        rec.run(steps={"db"}, loader=failing_loader)


def test_unknown_step_is_rejected(rec):
    with pytest.raises(ValueError, match="unknown step"):
        rec.run(steps={"score"})


def test_parse_steps_returns_pipeline_order_and_validates():
    assert parse_steps("db, manifest") == ("manifest", "db")
    assert parse_steps("fetch") == ("fetch",)
    with pytest.raises(ValueError):
        parse_steps("fetch,score")
    with pytest.raises(ValueError):
        parse_steps("")


def test_main_passes_steps_and_force(monkeypatch, capsys):
    seen = {}

    def fake_run(catalogue, *, steps, force):
        seen.update(catalogue=catalogue, steps=steps, force=force)
        return pipeline.PipelineOutcome("PROD", load_summary=SUMMARY)

    monkeypatch.setattr(pipeline, "run", fake_run)

    pipeline.main(["nsip-lu", "--steps", "fetch,db", "--force"])

    assert seen == {"catalogue": "nsip-lu", "steps": ("fetch", "db"), "force": True}
    assert "Database" in capsys.readouterr().out


def test_main_exits_1_with_a_message_on_input_error(monkeypatch, capsys):
    def fake_run(catalogue, *, steps, force):
        raise PipelineInputError("no files")

    monkeypatch.setattr(pipeline, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        pipeline.main(["nsip-lu", "--steps", "db"])

    assert exc.value.code == 1
    assert "no files" in capsys.readouterr().err


# --- stderr summary of an incomplete fetch ------------------------------------


def _stderr_for(monkeypatch, capsys, error):
    def fake_run(catalogue, *, steps, force):
        raise error

    monkeypatch.setattr(pipeline, "run", fake_run)
    with pytest.raises(SystemExit) as exc:
        pipeline.main(["nsip-lu"])
    assert exc.value.code == 1
    return capsys.readouterr().err


def test_main_with_exactly_the_cap_of_failures_shows_all_and_no_more_marker(
    monkeypatch, capsys
):
    err = _stderr_for(monkeypatch, capsys, _incomplete_error(n_failures=10))

    assert "id-9" in err
    assert "more" not in err


def test_main_with_one_failure_over_the_cap_hides_it_and_counts_it(monkeypatch, capsys):
    err = _stderr_for(monkeypatch, capsys, _incomplete_error(n_failures=11))

    assert "id-9" in err
    assert "id-10" not in err
    assert err.rstrip("\n").endswith(", ... and 1 more (see the report)")


def test_main_with_many_failures_counts_the_hidden_ones(monkeypatch, capsys):
    err = _stderr_for(monkeypatch, capsys, _incomplete_error(n_failures=15))

    assert "... and 5 more (see the report)" in err


def test_main_with_one_failure_has_the_documented_format(monkeypatch, capsys):
    err = _stderr_for(monkeypatch, capsys, _incomplete_error(n_failures=1))

    assert err == (
        "FetchIncompleteError: 1 of 20 resources failed"
        f" (report: {Path('out/report.xlsx')}): dataset id-0 (RuntimeError: boom-0)\n"
    )


def test_main_prints_one_line_even_when_a_cause_contains_newlines(monkeypatch, capsys):
    failures = [
        FetchFailure("dataset", "a", RuntimeError("line one\n  line two\r\nline three"))
    ]
    err = _stderr_for(
        monkeypatch,
        capsys,
        FetchIncompleteError(Path("out/report.xlsx"), failures, total=1),
    )

    assert err.count("\n") == 1 and err.endswith("\n")
    assert "line one line two line three" in err


def test_main_shows_the_underlying_cause_of_a_resource_fetch_error(monkeypatch, capsys):
    wrapped = ResourceFetchError("dataset", "a", ValueError("404 Not Found"))
    failures = [FetchFailure("dataset", "a", wrapped)]
    err = _stderr_for(
        monkeypatch,
        capsys,
        FetchIncompleteError(Path("out/report.xlsx"), failures, total=1),
    )

    assert "dataset a (ValueError: 404 Not Found)" in err
    assert "Failed to fetch" not in err


def test_main_shows_the_type_of_a_cause_with_an_empty_message(monkeypatch, capsys):
    failures = [FetchFailure("dataset", "a", KeyError())]
    err = _stderr_for(
        monkeypatch,
        capsys,
        FetchIncompleteError(Path("out/report.xlsx"), failures, total=1),
    )

    assert "dataset a (KeyError)" in err


# --- end to end with the real ingestor ----------------------------------------


def test_incomplete_fetch_end_to_end_exits_1_and_never_calls_the_loader(
    tmp_path, monkeypatch, capsys
):
    from tests.test_ingest import (
        FakeAuth,
        FakeResponse,
        _write_manifest,
        build_fetch_url,
    )

    manifests_dir = tmp_path / "manifests"
    fetched_dir = tmp_path / "fetched"
    _write_manifest(
        manifests_dir / "cat.xlsx",
        [("u-1", "dataset", "1"), ("u-2", "dataset", "2")],
    )
    auth = FakeAuth()
    auth.queue(build_fetch_url(auth.base_url, "dataset", "1"), FakeResponse(200, b"x"))
    auth.queue(
        build_fetch_url(auth.base_url, "dataset", "2"),
        *[FakeResponse(404) for _ in range(MAX_FETCH_ATTEMPTS)],
    )
    loader_calls = []

    def fake_loader(*args, **kwargs):
        loader_calls.append((args, kwargs))
        return SUMMARY

    real_run = pipeline.run

    def wired_run(catalogue, *, steps, force):
        return real_run(
            catalogue,
            steps=steps,
            force=force,
            env="PPE",
            manifests_dir=manifests_dir,
            fetched_data_dir=fetched_dir,
            db_path=fetched_dir / "mqa.sqlite",
            session_factory=lambda env: auth,
            loader=fake_loader,
        )

    monkeypatch.setattr(pipeline, "run", wired_run)

    with pytest.raises(SystemExit) as exc:
        pipeline.main(["cat", "--steps", "fetch,db"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "1 of 2 resources failed" in err
    assert str(fetched_dir / "PPE" / "cat" / "report.xlsx") in err
    assert "dataset 2" in err
    assert loader_calls == []


def test_db_only_never_builds_or_calls_the_ingestor(rec, tmp_path):
    (tmp_path / "fetched" / "PROD" / "cat").mkdir(parents=True)

    rec.run(steps={"db"})

    assert "ingestor" not in rec.names()
    assert "fetch" not in rec.names()
    assert "shutdown" not in rec.names()
