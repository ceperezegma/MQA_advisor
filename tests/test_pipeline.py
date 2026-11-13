"""Tests for the manifest -> fetch -> db pipeline with its step selector (no network, no DB)."""

from pathlib import Path

import pytest

from mqa_advisor import pipeline
from mqa_advisor.catalogue_db import LoadSummary
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
