"""Persistence invariants, failure injection and real process recovery without model calls."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.llm.accounting import UsageRecorder
from app.llm.summary import aggregate_usage
from app.storage import Conflict, NotFound, Storage, StorageError, WorkerBusy
from app.storage.validation import empty_data, validate_snapshot

ROOT = Path(__file__).resolve().parents[1]


def example(name="success"):
    return json.loads((ROOT / "contracts" / "examples" / f"{name}.json").read_text())


@pytest.fixture
def store(tmp_path):
    storage = Storage(tmp_path / "data")
    storage.create_project("Тест", example()["brief"]["input"], project_id="project_demo")
    storage.create_run("project_demo", example("stage_request")["limits"], run_id="run_demo")
    return storage


def result(stage="brief", *, with_data=False, artifact_id=None):
    fixture = example()
    artifact = fixture["artifacts"][0]
    artifact.update(id=artifact_id or "artifact_" + stage, stage=stage)
    if not with_data:
        artifact["entities"] = []
    return dict(schema_version="1.0.0", run_id="run_demo", project_id="project_demo", brief_version=1,
                stage=stage, outcome="success", data=fixture["data"] if with_data else empty_data(),
                artifacts=[artifact], questions=[], suggested_next_stage=None, usage_ids=[], error=None)


def begin(store, worker):
    run = store.get_run("run_demo")
    if run["status"] == "queued":
        run = worker.claim_run(run["id"])
    return worker.begin_step(run["id"], expected_revision=run["revision"])


def test_checkpoint_snapshot_and_usage_survive_new_storage_instance(store):
    with store.worker() as worker:
        job = begin(store, worker)
        event = UsageRecorder(store).record_tool(run_id="run_demo", stage="brief", provider="test", tool="search",
                                                duration_ms=3, cost={"amount": "0.12", "currency": "USD"})
        output = result(with_data=True)
        output["usage_ids"] = [event["id"]]
        checkpoint = worker.commit_step(job["id"], output, next_stage="research", graph_state={"repair_cycles": 0})
        assert worker.commit_step(job["id"], output, next_stage="research", graph_state={"repair_cycles": 0}) == checkpoint
        with pytest.raises(Conflict, match="different result"):
            worker.commit_step(job["id"], output, next_stage="options")
    restored = Storage(store.data_dir)
    snapshot = restored.get_snapshot("run_demo")
    validate_snapshot(snapshot)
    assert snapshot["data"] == output["data"]
    assert snapshot["artifacts"] == output["artifacts"]
    assert snapshot["usage"] == [event]
    assert restored.get_entity("project_demo", "decision", "decision_1", 1) == output["data"]["decisions"][0]
    assert restored.get_checkpoint(checkpoint["id"])["graph_state"] == {"repair_cycles": 0}
    assert restored.get_step_result(checkpoint["id"]) == output
    assert restored.list_jobs("run_demo")[0]["state"] == "completed"
    assert restored.get_limits("run_demo") == example("stage_request")["limits"]
    assert len(restored.list_runs("project_demo")) == 1


def test_single_worker_single_run_and_no_recovery_of_live_work(store):
    with store.worker() as worker:
        run = worker.claim_run("run_demo")
        with pytest.raises(WorkerBusy):
            with Storage(store.data_dir).worker():
                pass
        assert Storage(store.data_dir).recover_if_idle() == []
        assert store.get_run("run_demo") == run
        store.create_project("Другой", example()["brief"]["input"], project_id="project_second")
        store.create_run("project_second", example("stage_request")["limits"], run_id="run_second")
        with pytest.raises(Conflict, match="active run"):
            worker.claim_run("run_second")
        with pytest.raises(Conflict, match="unfinished"):
            store.create_run("project_demo", example("stage_request")["limits"])
        job = begin(store, worker)
        with pytest.raises(Conflict, match="already executing"):
            begin(store, worker)
        with pytest.raises(Conflict, match="revision"):
            worker.begin_step("run_demo", expected_revision=1)
    with pytest.raises(Conflict, match="not owned"):
        worker.commit_step(job["id"], result(), next_stage="research")


@pytest.mark.parametrize("failure_point", ["result", "checkpoint", "run_update"])
def test_failed_commit_keeps_previous_checkpoint_and_charged_usage(store, monkeypatch, failure_point):
    with store.worker() as worker:
        first = worker.commit_step(begin(store, worker)["id"], result(), next_stage="research")
        job = begin(store, worker)
        event = UsageRecorder(store).record_tool(run_id="run_demo", stage="research", provider="test", tool="search", duration_ms=1)
        output = result("research", with_data=True)
        output["usage_ids"] = [event["id"]]
        before = store.get_snapshot("run_demo")
        real_materialize = store._materialize
        real_write_run = store._write_run

        def fail_file(directory, payload):
            if directory == ("results" if failure_point == "result" else "checkpoints"):
                raise OSError("injected disk failure")
            return real_materialize(directory, payload)

        def fail_update(connection, run):
            real_write_run(connection, run)
            raise OSError("injected transaction failure")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_write_run" if failure_point == "run_update" else "_materialize",
                          fail_update if failure_point == "run_update" else fail_file)
            with pytest.raises(OSError, match="injected"):
                worker.commit_step(job["id"], output, next_stage="options")
        assert store.get_snapshot("run_demo") == before
        with pytest.raises(NotFound):
            store.get_entity("project_demo", "option", "option_1", 1)
        assert store.get_run("run_demo")["checkpoint_id"] == first["id"]
        checkpoint = worker.commit_step(job["id"], output, next_stage="options")
        assert checkpoint["parent_id"] == first["id"]
        assert len(store.list_usage("run_demo")) == 1


def test_mirrors_can_be_rebuilt_and_ids_never_become_paths(store):
    with store.worker() as worker:
        checkpoint = worker.commit_step(begin(store, worker)["id"], result(), next_stage="research")
    files = store.checkpoint_files(checkpoint["id"])
    files["result"].unlink()
    files["checkpoint"].write_text("damaged")
    restored = Storage(store.data_dir).checkpoint_files(checkpoint["id"])
    assert json.loads(restored["result"].read_text()) == result()
    assert json.loads(restored["checkpoint"].read_text()) == checkpoint
    assert all(len(path.stem) == 64 for path in restored.values())
    with pytest.raises(StorageError):
        store.create_project("Bad", example()["brief"]["input"], project_id="../../escape")


def test_usage_is_idempotent_and_duplicate_call_attempts_are_rejected(store):
    event = UsageRecorder(store).record_tool(run_id="run_demo", stage="brief", provider="test", tool="search",
                                            duration_ms=2, cost={"amount": "0.1", "currency": "USD"})
    store.record(deepcopy(event))
    changed = dict(event, duration_ms=3)
    with pytest.raises(Conflict):
        store.record(changed)
    with pytest.raises(Conflict):
        store.record(dict(event, id="usage_duplicate"))
    with pytest.raises(NotFound):
        store.record(dict(event, id="usage_missing", run_id="run_missing"))
    with pytest.raises(StorageError):
        store.record(dict(event, id="usage_invalid", cost=None))
    totals = aggregate_usage(store.list_usage("run_demo"))["totals"]
    assert totals["events"] == 1
    assert totals["cost_by_currency"]["USD"]["known_total"] == "0.1"


@pytest.mark.parametrize("mutation", ["run", "brief", "entity", "artifact_run", "usage", "reference", "cycle", "schema", "outcome"])
def test_invalid_result_rolls_back_whole_step(store, mutation):
    with store.worker() as worker:
        job = begin(store, worker)
        output = result(with_data=True)
        if mutation == "run":
            output["run_id"] = "run_other"
        elif mutation == "brief":
            output["brief_version"] = 2
        elif mutation == "entity":
            output["data"]["options"][0]["project_id"] = "project_other"
        elif mutation == "artifact_run":
            output["artifacts"][0]["run_id"] = "run_other"
        elif mutation == "usage":
            output["usage_ids"] = ["usage_unknown"]
        elif mutation == "reference":
            output["artifacts"][0]["entities"][0]["version"] = 2
        elif mutation == "cycle":
            output["artifacts"][0]["depends_on"] = [{"artifact_id": "artifact_brief", "version": 1}]
        elif mutation == "schema":
            output["private_extra"] = "not logged"
        else:
            output["outcome"] = "incomplete"
        before = store.get_snapshot("run_demo")
        with pytest.raises(StorageError):
            worker.commit_step(job["id"], output, next_stage="research")
        assert store.get_snapshot("run_demo") == before
        assert not list((store.data_dir / "results").glob("*.json"))
        with pytest.raises(NotFound):
            store.get_entity("project_demo", "option", "option_1", 1)


def test_entity_history_and_stale_artifacts_are_preserved(store):
    with store.worker() as worker:
        worker.commit_step(begin(store, worker)["id"], result(with_data=True), next_stage="research")
        second = result("research", with_data=True)
        second["data"]["options"][0].update(version=2, rationale="New version")
        second["artifacts"][0]["entities"][0]["version"] = 2
        worker.commit_step(begin(store, worker)["id"], second, next_stage="report")
        snapshot = store.get_snapshot("run_demo")
        assert snapshot["artifacts"][0]["freshness"] == "stale"
        assert store.get_entity("project_demo", "option", "option_1", 1)["rationale"] != "New version"
        assert store.get_entity("project_demo", "option", "option_1", 2)["rationale"] == "New version"
        # Current report can coexist with a stale artifact whose entity is only in the archive.
        final = result("report")
        worker.commit_step(begin(store, worker)["id"], final, status="completed", quality="ready")
        validate_snapshot(store.get_snapshot("run_demo"))
    with pytest.raises(Conflict, match="version"):
        store.append_brief("project_demo", example()["brief"]["input"], expected_version=2)
    updated_input = example()["brief"]["input"]
    updated_input["budget"]["amount"] = "50000"
    assert store.append_brief("project_demo", updated_input, expected_version=1)["version"] == 2
    assert store.get_brief("project_demo", 1)["input"]["budget"]["amount"] == "100000"
    assert store.get_brief("project_demo")["version"] == 2
    # A historical run keeps its own brief even after the project's current version advances.
    assert store.get_snapshot("run_demo")["brief"]["version"] == 1
    with pytest.raises(Conflict):
        store.create_run("project_demo", example("stage_request")["limits"], brief_version=1)
    assert store.create_run("project_demo", example("stage_request")["limits"])["brief_version"] == 2


def test_immutable_entity_and_artifact_versions(store):
    with store.worker() as worker:
        worker.commit_step(begin(store, worker)["id"], result(with_data=True), next_stage="brief")
        job = begin(store, worker)
        output = result(with_data=True)
        output["data"]["options"][0]["rationale"] = "Changed without version"
        with pytest.raises(Conflict, match="immutable"):
            worker.commit_step(job["id"], output, next_stage="research")
        output = result(with_data=True)
        output["artifacts"][0]["summary"] = "Changed without version"
        with pytest.raises(Conflict, match="immutable"):
            worker.commit_step(job["id"], output, next_stage="research")
        with pytest.raises(Conflict, match="unfinished"):
            store.append_brief("project_demo", example()["brief"]["input"], expected_version=1)


@pytest.mark.parametrize("fixture,status", [("stage_result", "waiting_for_input"), ("search_error", "failed")])
def test_questions_and_error_survive_recovery(store, fixture, status):
    output = example("stage_result")
    if fixture == "search_error":
        output.update(outcome="failed", questions=[], error=example("search_error")["run"]["error"])
    with store.worker() as worker:
        worker.commit_step(begin(store, worker)["id"], output, status=status)
    assert store.recover_if_idle() == []
    snapshot = Storage(store.data_dir).get_snapshot("run_demo")
    assert snapshot["run"]["status"] == status
    assert snapshot["questions"] == output["questions"]
    assert snapshot["run"]["error"] == output["error"]


@pytest.mark.parametrize("kill_before_commit", [False, True])
def test_sigkill_releases_worker_and_restores_last_confirmed_step(store, tmp_path, kill_before_commit):
    # The child is paused at a pipe handshake, never by timing-dependent sleeps.
    (tmp_path / "output.json").write_text(json.dumps(result(with_data=True)))
    child_code = r'''
import json, sys
from pathlib import Path
from app.storage import Storage
store = Storage(Path(sys.argv[1]))
with store.worker() as worker:
    run = worker.claim_run("run_demo")
    job = worker.begin_step("run_demo", expected_revision=run["revision"])
    first = json.loads(Path(sys.argv[2]).read_text())
    checkpoint = worker.commit_step(job["id"], first, next_stage="research", graph_state={"repair_cycles": 1})
    job = worker.begin_step("run_demo", expected_revision=store.get_run("run_demo")["revision"])
    if sys.argv[3] == "True":
        original = store._write_run
        def stop_before_commit(connection, run):
            original(connection, run)
            print(json.dumps({"checkpoint_id": checkpoint["id"], "job_id": job["id"]}), flush=True)
            sys.stdin.readline()
        store._write_run = stop_before_commit
        output = dict(first, stage="research", artifacts=[])
        worker.commit_step(job["id"], output, next_stage="options")
    else:
        print(json.dumps({"checkpoint_id": checkpoint["id"], "job_id": job["id"]}), flush=True)
        sys.stdin.readline()
'''
    process = subprocess.Popen([sys.executable, "-E", "-c", child_code, str(store.data_dir),
                                str(tmp_path / "output.json"), str(kill_before_commit)],
                               cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([process.stdout], [], [], 15)[0], "Child did not reach the crash checkpoint"
        line = process.stdout.readline()
        assert line, process.stderr.read()
        saved = json.loads(line)
        if not kill_before_commit:
            # A different process must be refused ownership while the child is alive.
            with pytest.raises(WorkerBusy):
                with store.worker():
                    pass
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    # Recovery itself occurs in a new interpreter, not just a fresh Python object.
    recovery = subprocess.run([sys.executable, "-E", "-c", '''
import json, sys
from pathlib import Path
from app.storage import Storage
store = Storage(Path(sys.argv[1]))
print(json.dumps(store.recover_if_idle()))
''', str(store.data_dir)], cwd=ROOT, capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(recovery.stdout) == ["run_demo"]
    restored = Storage(store.data_dir)
    snapshot = restored.get_snapshot("run_demo")
    assert snapshot["run"]["status"] == "paused"
    assert snapshot["run"]["stage"] == "research"
    assert snapshot["run"]["checkpoint_id"] == saved["checkpoint_id"]
    assert snapshot["data"] == result(with_data=True)["data"]
    assert restored.get_checkpoint(saved["checkpoint_id"])["graph_state"] == {"repair_cycles": 1}
    assert [job["state"] for job in restored.list_jobs("run_demo")] == ["completed", "interrupted"]
    with restored.worker() as worker:
        with pytest.raises(Conflict, match="not owned"):
            worker.commit_step(saved["job_id"], result("research"), next_stage="options")
        with pytest.raises(Conflict, match="revision"):
            restored.requeue_paused("run_demo", expected_revision=1)
        restored.requeue_paused("run_demo", expected_revision=snapshot["run"]["revision"])
        worker.claim_run("run_demo")
        checkpoint = worker.commit_step(begin(restored, worker)["id"], result("research"), next_stage="options")
        assert checkpoint["parent_id"] == saved["checkpoint_id"]
    assert len(restored.list_jobs("run_demo")) == 3
