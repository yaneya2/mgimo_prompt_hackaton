"""Behavioral A5 checks: full graph, human gates, repair budgets, failures and worker processes."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
from threading import Event
import time

import pytest

from app.config import Settings
from app.llm.accounting import UsageRecorder
from app.llm.base import ModelError
from app.storage import Conflict, Storage, StorageError
from app.storage.validation import validate_snapshot
from app.worker import WorkerService
from app.workflow.demo import build_stages, demo_stage, ref
from app.workflow.stages import RecordingSink, StageRegistry, StageSpec, load_registry
from scripts.try_workflow import LIMITS, demo_brief, run_demo

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path):
    store = Storage(tmp_path / "state")
    brief = demo_brief("Новый продукт вне примера: учёт расходников мастерской")
    brief["budget"] = {"amount": "50000", "currency": "RUB"}
    project = store.create_project("Тест A5", brief)
    run = store.create_run(project["id"], LIMITS)
    return store, run["id"]


def registry_with(stage, handler, *, registry=None):
    registry = registry or build_stages()
    stages = dict(registry.stages)
    stages[stage] = replace(stages[stage], handler=handler)
    return replace(registry, stages=stages)


def execute(setup, registry=None, stop=None):
    store, run_id = setup
    WorkerService(store, registry or build_stages()).run(once=True, stop=stop)
    return store.get_snapshot(run_id)


def choose(store, snapshot):
    option = snapshot["data"]["options"][0]
    return store.submit_selection(snapshot["run"]["id"], option["id"], option["version"], "Решение пользователя",
                                  expected_revision=snapshot["run"]["revision"])


def test_complete_graph_waits_for_user_and_preserves_every_stage(setup):
    store, run_id = setup
    first = execute(setup)
    assert first["run"]["stage"] == "selection" and first["run"]["status"] == "waiting_for_input"
    assert not first["data"]["decisions"]
    assert WorkerService(store, build_stages()).run(once=True) == 0
    choose(store, first)
    assert store.get_snapshot(run_id)["questions"] == []
    final = execute(setup)
    assert final["run"]["status"] == "completed" and final["run"]["quality"] == "draft"
    assert final["data"]["decisions"][0]["author"] == "user"
    assert {item["stage"] for item in final["artifacts"]} == {"brief", "research", "options", "communications", "review", "selection", "report"}
    jobs = store.list_jobs(run_id)
    assert [job["stage"] for job in jobs] == ["brief", "research", "options", "communications", "review", "selection", "selection", "report"]
    assert all(job["state"] == "completed" and job["checkpoint_id"] for job in jobs)
    assert all(store.checkpoint_files(job["checkpoint_id"])["result"].is_file() for job in jobs)
    validate_snapshot(final)
    assert store.list_usage(run_id) == []


def test_saved_answers_are_delivered_after_restart(tmp_path):
    store = Storage(tmp_path)
    project = store.create_project("Clarification", demo_brief())
    run = store.create_run(project["id"], LIMITS)
    first = execute((store, run["id"]))
    assert first["run"]["stage"] == "brief" and first["questions"]
    revision = first["run"]["revision"]
    for answers in ([], [{"question_id": "question_unknown", "value": "bad"}],
                    [{"question_id": "question_budget", "value": " "}],
                    [{"question_id": "question_budget", "value": "a"}, {"question_id": "question_budget", "value": "b"}]):
        with pytest.raises(StorageError):
            store.submit_answers(run["id"], answers, expected_revision=revision)
    with pytest.raises(Conflict):
        store.submit_answers(run["id"], [{"question_id": "question_budget", "value": "1000 RUB"}], expected_revision=1)
    store.submit_answers(run["id"], [{"question_id": "question_budget", "value": "1000 RUB"}], expected_revision=revision)
    with pytest.raises(Conflict):
        store.submit_answers(run["id"], [{"question_id": "question_budget", "value": "2000 RUB"}], expected_revision=revision)
    restored = Storage(tmp_path)
    snapshot = execute((restored, run["id"]))
    assert snapshot["run"]["stage"] == "selection"
    assert "1000 RUB" in snapshot["data"]["sources"][0]["content_excerpt"]
    assert len(restored.list_inputs(run["id"])) == 1
    saved_input = restored.list_inputs(run["id"])[0]
    assert saved_input["questions"] == first["questions"]
    assert saved_input["stage"] == "brief" and saved_input["brief_version"] == 1


def test_selection_rejects_outdated_options_and_wrong_command(setup):
    store, run_id = setup
    snapshot = execute(setup)
    option = snapshot["data"]["options"][0]
    with pytest.raises(Conflict):
        store.submit_selection(run_id, option["id"], option["version"] + 1, "Reason", expected_revision=snapshot["run"]["revision"])
    with pytest.raises(Conflict):
        store.submit_answers(run_id, [{"question_id": "question_selection", "value": option["id"]}], expected_revision=snapshot["run"]["revision"])
    assert not store.list_inputs(run_id)


@pytest.mark.parametrize("target", ["research", "options", "communications"])
def test_review_routes_to_requested_stage_then_rechecks(setup, target):
    store, run_id = setup
    calls = []

    def critic(request, context):
        calls.append(request["stage"])
        output = demo_stage(request, context)
        if not request["input_data"]["issues"]:
            issue = dict(id="issue_test", version=1, project_id=request["project_id"], brief_version=1,
                         code="TEST_REPAIR", severity="blocking", target=ref("claim", request["input_data"]["claims"][0]),
                         explanation="Тестовая проверка возврата", evidence=[], suggested_fix="Исправить тестовый результат",
                         return_to=target, status="open")
            output["data"]["issues"] = [issue]
            output["artifacts"][0]["entities"].append(ref("issue", issue))
            # Even a misleading success must not bypass a blocking issue.
            output["outcome"] = "success"
        return output

    registry = registry_with("review", critic)
    snapshot = execute(setup, registry)
    assert snapshot["run"]["status"] == "waiting_for_input"
    assert len(calls) == 2
    assert snapshot["data"]["issues"][0]["status"] == "resolved"
    checkpoint = store.get_checkpoint(snapshot["run"]["checkpoint_id"])
    assert checkpoint["graph_state"]["repairs_used"] == 1
    jobs = store.list_jobs(run_id)
    first_review = next(i for i, job in enumerate(jobs) if job["stage"] == "review")
    assert jobs[first_review + 1]["stage"] == target
    choose(store, snapshot)
    assert execute(setup, registry)["run"]["status"] == "completed"


def test_repair_budget_exhaustion_preserves_blocking_issue(setup):
    def critic(request, context):
        output = demo_stage(request, context)
        old = request["input_data"]["issues"]
        issue = dict(id="issue_persistent", version=old[0]["version"] + 1 if old else 1,
                     project_id=request["project_id"], brief_version=1, code="UNRESOLVED", severity="blocking",
                     target=ref("claim", request["input_data"]["claims"][0]), explanation="Тестовый постоянный дефект",
                     evidence=[], suggested_fix="Нужно новое решение", return_to="communications", status="accepted_limitation")
        output["data"]["issues"] = [issue]
        output["artifacts"][0]["entities"] = [ref("issue", issue)]
        output["outcome"] = "needs_revision"
        return output

    store, run_id = setup
    snapshot = execute(setup, registry_with("review", critic))
    assert snapshot["run"]["status"] == "incomplete" and snapshot["run"]["quality"] == "blocked"
    assert snapshot["data"]["issues"][0]["status"] == "accepted_limitation"
    assert len([job for job in store.list_jobs(run_id) if job["stage"] == "review"]) == 3
    checkpoint = store.get_checkpoint(snapshot["run"]["checkpoint_id"])
    assert checkpoint["graph_state"]["stop_reason"]["code"] == "REPAIR_LIMIT_EXCEEDED"
    assert any("Лимит" in item["summary"] for item in snapshot["artifacts"])


@pytest.mark.parametrize("mutation", ["schema", "cross_run", "reference", "user_decision", "nonfinite", "no_artifact"])
def test_invalid_output_is_never_forwarded_and_attempts_are_bounded(setup, mutation):
    called = []

    def invalid(request, context):
        called.append(list(context.feedback))
        output = demo_stage(request, context)
        if mutation == "schema":
            output["private"] = "sensitive invalid text"
        elif mutation == "cross_run":
            output["run_id"] = "run_wrong"
        elif mutation == "reference":
            output["artifacts"][0]["entities"][0]["id"] = "source_missing"
        elif mutation == "user_decision":
            output["data"]["decisions"] = [dict(id="decision_forged", version=1, project_id=request["project_id"],
                                                brief_version=1, option_id="option_fake", action="select", author="user",
                                                rationale="sensitive invalid text", created_at="2026-11-10T00:00:00Z")]
        elif mutation == "nonfinite":
            output["data"]["sources"][0]["version"] = float("nan")
        else:
            output["artifacts"] = []
        return output

    store, run_id = setup
    snapshot = execute(setup, registry_with("brief", invalid))
    assert snapshot["run"]["status"] == "incomplete"
    assert len(called) == LIMITS["max_repair_cycles"] + 1
    assert called == [[], ["INVALID_STAGE_RESULT"], ["INVALID_STAGE_RESULT"]]
    assert all(job["stage"] == "brief" for job in store.list_jobs(run_id))
    assert not snapshot["data"]["sources"]
    for job in store.list_jobs(run_id):
        assert "sensitive invalid text" not in json.dumps(store.get_step_result(job["checkpoint_id"]))


def test_invalid_answer_can_be_corrected_and_suggestion_cannot_skip_stages(setup):
    attempts = []

    def repaired(request, context):
        attempts.append(context.repairs_used)
        if not context.feedback:
            return {"invalid": True}
        output = demo_stage(request, context)
        output["suggested_next_stage"] = "report"
        return output

    snapshot = execute(setup, registry_with("brief", repaired))
    assert snapshot["run"]["stage"] == "selection" and snapshot["run"]["status"] == "waiting_for_input"
    assert attempts == [0, 1]
    assert len(snapshot["data"]["options"]) == 3


def test_repair_counter_survives_paused_worker_and_new_instance(setup):
    store, run_id = setup
    stop = Event()

    def invalid(request, context):
        stop.set()
        return {"bad": True}

    registry = registry_with("brief", invalid)
    first = execute(setup, registry, stop=stop)
    assert first["run"]["status"] == "paused"
    checkpoint = store.get_checkpoint(first["run"]["checkpoint_id"])
    assert checkpoint["graph_state"]["repairs_used"] == 1
    store.requeue_paused(run_id, expected_revision=first["run"]["revision"])
    second_registry = registry_with("brief", lambda request, context: {"bad": True})
    final = execute((Storage(store.data_dir), run_id), second_registry)
    assert final["run"]["status"] == "incomplete"
    assert len(store.list_jobs(run_id)) == 3


@pytest.mark.parametrize("error", [RuntimeError("private response and secret"), TimeoutError("private response"),
                                 ModelError("PROVIDER_ERROR", "secret provider diagnostic", retryable=True)])
def test_handler_errors_fail_safely_and_keep_usage(setup, error):
    store, run_id = setup

    def failing(request, context):
        UsageRecorder(store).record_tool(run_id=run_id, stage="brief", provider="test", tool="search", duration_ms=4, outcome="error")
        raise error

    snapshot = execute(setup, registry_with("brief", failing))
    assert snapshot["run"]["status"] == "failed"
    assert len(snapshot["usage"]) == 1
    assert len(store.list_jobs(run_id)) == 1
    assert "private" not in json.dumps(snapshot) and "secret" not in json.dumps(snapshot)


def test_disk_failure_does_not_cause_a_second_handler_call(setup, monkeypatch):
    store, run_id = setup
    calls = []

    def counted(request, context):
        calls.append(request["stage"])
        return demo_stage(request, context)

    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_materialize", broken)
    with pytest.raises(OSError):
        execute(setup, registry_with("brief", counted))
    assert calls == ["brief"]
    assert store.get_run(run_id)["checkpoint_id"] is None
    assert store.list_jobs(run_id)[0]["state"] == "running"


def test_registry_configuration_cannot_silently_change_during_run(setup):
    store, run_id = setup
    first = execute(setup)
    choose(store, first)
    changed = replace(build_stages(), name="different-model-and-prompts")
    final = execute(setup, changed)
    assert final["run"]["status"] == "failed"
    assert final["run"]["error"]["error"]["code"] == "INVALID_STATE"


@pytest.mark.parametrize("change_selection", [False, True])
def test_ready_report_requires_unchanged_human_selection(setup, change_selection):
    store, run_id = setup
    # Fake handlers with the production readiness policy; still no external services.
    registry = replace(build_stages(), name="readiness-policy-test", synthetic=False)
    if change_selection:
        def changing_report(request, context):
            output = demo_stage(request, context)
            option = deepcopy(request["input_data"]["options"][0])
            option.update(version=option["version"] + 1, rationale="Changed after selection")
            output["data"]["options"] = [option]
            # Even omitting dependencies must not bypass the exact selected version.
            output["artifacts"][0].update(depends_on=[], entities=[])
            return output
        registry = registry_with("report", changing_report, registry=registry)
    waiting = execute(setup, registry)
    choose(store, waiting)
    final = execute(setup, registry)
    if change_selection:
        assert final["run"]["status"] == "incomplete"
        assert final["run"]["quality"] == "blocked"
        assert final["data"]["options"][0]["version"] == 1
    else:
        assert final["run"]["status"] == "completed" and final["run"]["quality"] == "ready"


def test_version_one_database_is_migrated_without_losing_runs(tmp_path):
    store = Storage(tmp_path)
    brief = demo_brief()
    brief["budget"] = {"amount": "1000", "currency": "RUB"}
    project = store.create_project("Preserved", brief)
    run = store.create_run(project["id"], LIMITS)
    stop = Event()

    def pause_after_brief(request, context):
        stop.set()
        return demo_stage(request, context)

    before = execute((store, run["id"]), registry_with("brief", pause_after_brief), stop=stop)
    jobs_before = store.list_jobs(run["id"])
    checkpoint_before = store.get_checkpoint(before["run"]["checkpoint_id"])
    database = tmp_path / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        # Reconstruct A4 before the input, command replay and export tables were added.
        connection.execute("DROP TABLE run_inputs")
        connection.execute("DROP TABLE commands")
        connection.execute("DROP TABLE exports")
        for table in ('external_calls', 'response_cache', 'run_events'):
            connection.execute('DROP TABLE ' + table)
        connection.execute("PRAGMA user_version = 1")
    store = Storage(tmp_path)
    assert store.get_snapshot(run["id"]) == before
    assert store.list_jobs(run["id"]) == jobs_before
    assert store.get_checkpoint(before["run"]["checkpoint_id"]) == checkpoint_before
    assert store.list_inputs(run["id"]) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert Storage(tmp_path).list_projects() == store.list_projects()


def test_explicit_factory_registration_and_missing_configuration(setup):
    store, _ = setup
    settings = Settings(_env_file=None)
    assert load_registry("app.workflow.demo:build_stages", settings, RecordingSink(store.record)).synthetic
    with pytest.raises(ValueError, match="APP_STAGE_FACTORY"):
        load_registry(None, settings, RecordingSink(store.record))
    with pytest.raises(ValueError):
        StageRegistry("missing", {"brief": StageSpec(demo_stage, "v1", "test")})


def test_demo_completes_in_three_separate_workers(tmp_path):
    report = run_demo(tmp_path, product="Условный инструмент учёта книг")
    assert report["jobs"] == 9 and report["usage_events"] == 0
    snapshot = json.loads(Path(report["snapshot"]).read_text())
    assert snapshot["run"]["status"] == "completed"
    assert snapshot["data"]["decisions"][0]["author"] == "user"
    assert snapshot["brief"]["input"]["product"] == "Условный инструмент учёта книг"


def test_worker_killed_during_research_resumes_without_repeating_brief(setup):
    store, run_id = setup
    script = '''
from dataclasses import replace
from pathlib import Path
import sys
from app.storage import Storage
from app.worker import WorkerService
from app.workflow.demo import build_stages
store = Storage(Path(sys.argv[1]))
registry = build_stages()
def block(request, context):
    print("research-started", flush=True)
    sys.stdin.readline()
stages = dict(registry.stages)
stages["research"] = replace(stages["research"], handler=block)
WorkerService(store, replace(registry, stages=stages)).run(once=True)
'''
    process = subprocess.Popen([sys.executable, "-E", "-c", script, str(store.data_dir)], cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 15)[0]
        assert process.stdout.readline().strip() == "research-started"
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    command = [sys.executable, "-E", "-m", "app.worker", "--stages", "demo", "--once", "--data-dir", str(store.data_dir)]
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, timeout=15)
    run = store.get_run(run_id)
    assert run["status"] == "paused" and run["stage"] == "research"
    store.requeue_paused(run_id, expected_revision=run["revision"])
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, timeout=15)
    jobs = store.list_jobs(run_id)
    assert [job["stage"] for job in jobs].count("brief") == 1
    assert [job["stage"] for job in jobs].count("research") == 2
    assert jobs[1]["state"] == "interrupted"
    assert store.get_run(run_id)["status"] == "waiting_for_input"


def test_continuous_worker_accepts_input_without_http_and_stops_on_sigterm(setup):
    store, run_id = setup
    command = [sys.executable, "-E", "-m", "app.worker", "--stages", "demo", "--data-dir", str(store.data_dir)]
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_for(status):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            snapshot = store.get_snapshot(run_id)
            if snapshot["run"]["status"] == status:
                return snapshot
            if process.poll() is not None:
                pytest.fail("Worker exited: " + process.stderr.read())
            time.sleep(0.02)
        pytest.fail("Worker did not reach " + status)

    try:
        snapshot = wait_for("waiting_for_input")
        # A second worker exits without claiming or recovering the live one's work.
        other = subprocess.run(command + ["--once"], cwd=ROOT, capture_output=True, text=True, timeout=15)
        assert other.returncode == 2 and "Another worker" in other.stderr
        choose(store, snapshot)
        final = wait_for("completed")
        assert final["run"]["stage"] == "report"
        process.terminate()
        process.communicate(timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
