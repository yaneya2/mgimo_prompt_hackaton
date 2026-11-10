"""Persisted, bounded routing. Agents cannot choose statuses or bypass human selection."""
from copy import deepcopy
import json

from app.llm.base import ModelError
from app.storage.database import encode
from app.storage.validation import COLLECTIONS, StorageError, require, validate
from app.workflow.results import artifact_for, error_for, result_for
from app.workflow.stages import STAGES, StageContext, StageSpec

NEXT = dict(zip(STAGES, STAGES[1:]))
REPAIR_TARGETS = ("research", "options", "communications")


class Orchestrator:
    def __init__(self, storage, registry):
        self.storage = storage
        self.registry = registry

    def _request(self, run):
        snapshot = self.storage.get_snapshot(run["id"])
        spec = self.registry.stages.get(run["stage"], StageSpec(self._selection, "platform-selection-v1", "platform"))
        request = dict(schema_version="1.0.0", run_id=run["id"], project_id=run["project_id"], stage=run["stage"],
                       brief=snapshot["brief"], input_data=snapshot["data"], input_artifacts=snapshot["artifacts"],
                       limits=self.storage.get_limits(run["id"]), prompt_version=spec.prompt_version, model_id=spec.model_id)
        validate("StageRequest", request)
        return request, spec

    def _state(self, run):
        state = self.storage.get_checkpoint(run["checkpoint_id"])["graph_state"] if run["checkpoint_id"] else {}
        return dict(workflow_version=1, repairs_used=state.get("repairs_used", 0), feedback=state.get("feedback", []),
                    registry=state.get("registry", self.registry.manifest()), stop_reason=None)

    @staticmethod
    def _merged_data(request, result):
        data = {}
        for collection in COLLECTIONS.values():
            items = {item["id"]: item for item in request["input_data"][collection]}
            items.update({item["id"]: item for item in result["data"][collection]})
            data[collection] = list(items.values())
        return data

    def _check_output(self, request, output):
        validate("StageResult", output)
        require((output["run_id"], output["project_id"], output["brief_version"], output["stage"])
                == (request["run_id"], request["project_id"], request["brief"]["version"], request["stage"]),
                "Stage context mismatch")
        if output["outcome"] == "success":
            require(bool(output["artifacts"]), "A successful stage requires a saved artifact")
        if output["outcome"] != "needs_input":
            require(not output["questions"], "Questions require needs_input")
        previous = {item["id"]: item for item in request["input_data"]["decisions"]}
        if request["stage"] != "selection":
            require(all(item["author"] != "user" or previous.get(item["id"]) == item
                        for item in output["data"]["decisions"]), "An agent cannot create a user decision")
        for artifact in output["artifacts"]:
            require(artifact["prompt_version"] == request["prompt_version"], "Wrong prompt version")
        if request["stage"] == "options" and output["outcome"] == "success":
            require(len({item["id"] for item in self._merged_data(request, output)["options"]}) >= 3,
                    "Options requires at least three alternatives")

    def _selection(self, request, context):
        inputs = [item for item in context.inputs if item["kind"] == "selection"]
        run = self.storage.get_run(request["run_id"])
        submitted = next((item for item in reversed(inputs) if item["checkpoint_id"] == run["checkpoint_id"]), None)
        options = request["input_data"]["options"]
        require(bool(options), "Selection requires options")
        if submitted is None:
            question = dict(id="question_selection", stage="selection", text="Выберите продуктовое направление.",
                            required=True, options=[item["id"] + ": " + item["name"] for item in options])
            return result_for(request, outcome="needs_input", questions=[question])
        choice = submitted["payload"]
        option = next((item for item in options if item["id"] == choice["option_id"]
                       and item["version"] == choice["option_version"]), None)
        require(option is not None, "Selected option changed")
        decision = dict(id="decision_" + submitted["id"].removeprefix("input_"), version=1,
                        project_id=request["project_id"], brief_version=request["brief"]["version"],
                        option_id=option["id"], action="select", author="user", rationale=choice["rationale"],
                        created_at=submitted["created_at"])
        output = result_for(request)
        output["data"]["decisions"] = [decision]
        output["artifacts"] = [artifact_for(request, "Пользователь выбрал направление: " + option["name"], entities=[
            dict(kind="decision", id=decision["id"], version=1),
            dict(kind="option", id=option["id"], version=option["version"])])]
        return output

    @staticmethod
    def _repair(state, limit, code, message):
        if state["repairs_used"] >= limit:
            state["stop_reason"] = dict(code="REPAIR_LIMIT_EXCEEDED", message=message)
            state["feedback"] = [code]
            return False
        state["repairs_used"] += 1
        state["feedback"] = [code]
        return True

    def _route(self, request, output, state):
        outcome, stage = output["outcome"], request["stage"]
        data = self._merged_data(request, output)
        blocking = [item for item in data["issues"] if item["severity"] == "blocking" and item["status"] != "resolved"]
        blocked = bool(blocking) or any(item["status"] == "blocked" for item in data["messages"])
        quality = "blocked" if blocked else "draft"
        if outcome in ("failed", "incomplete", "needs_input"):
            return dict(status={"failed": "failed", "incomplete": "incomplete", "needs_input": "waiting_for_input"}[outcome],
                        quality=quality)
        if outcome == "needs_revision" or (stage == "review" and blocked):
            target = stage
            if stage == "review":
                issues = blocking or [item for item in data["issues"] if item["status"] == "open"]
                targets = {item["return_to"] for item in issues}
                if not targets or not targets <= set(REPAIR_TARGETS):
                    output["outcome"] = "incomplete"
                    state["stop_reason"] = dict(code="NO_REPAIR_ROUTE", message="Критик не указал допустимый этап исправления.")
                    return dict(status="incomplete", quality="blocked")
                target = min(targets, key=REPAIR_TARGETS.index)
            if self._repair(state, request["limits"]["max_repair_cycles"], "REVIEW_REVISION" if stage == "review" else "STAGE_REVISION",
                            "Лимит исправлений исчерпан. Требуется решение пользователя или изменение условий."):
                output["outcome"] = "needs_revision"
                return dict(status="running", next_stage=target, quality=quality)
            output["outcome"] = "incomplete"
            return dict(status="incomplete", quality="blocked")
        state["feedback"] = []
        if stage == "report":
            require(any(item["author"] == "user" and item["action"] == "select" for item in data["decisions"]),
                    "Report requires a user decision")
            choices = [item["payload"] for item in self.storage.list_inputs(request["run_id"]) if item["kind"] == "selection"]
            require(bool(choices), "Report requires a saved human selection")
            choice = choices[-1]
            require(any(item["id"] == choice["option_id"] and item["version"] == choice["option_version"]
                        for item in data["options"]), "Report cannot change the selected option version")
            if blocked:
                output["outcome"] = "incomplete"
                state["stop_reason"] = dict(code="BLOCKING_ISSUES", message="Итог содержит неустранённые блокирующие замечания.")
                return dict(status="incomplete", quality="blocked")
            return dict(status="completed", quality="draft" if self.registry.synthetic else "ready")
        return dict(status="running", next_stage=NEXT[stage], quality=quality)

    def _diagnostic(self, request, state, *, message, outcome="incomplete", error=None):
        request = dict(request, prompt_version="platform-diagnostic-v1")
        output = result_for(request, outcome=outcome, error=error)
        output["artifacts"] = [artifact_for(request, message, namespace="diagnostic", dependencies=False)]
        return output

    def _invalid(self, worker, job, request, state):
        message = "Ответ этапа не прошёл проверку формата или связей. Невалидные данные не переданы дальше."
        retry = self._repair(state, request["limits"]["max_repair_cycles"], "INVALID_STAGE_RESULT", message + " Лимит исправлений исчерпан.")
        output = self._diagnostic(request, state, message=message if retry else state["stop_reason"]["message"],
                                  outcome="needs_revision" if retry else "incomplete")
        return worker.commit_step(job["id"], output, status="running" if retry else "incomplete",
                                  next_stage=request["stage"] if retry else None, quality="blocked", graph_state=state)

    def _failed(self, worker, job, request, state, code, message, *, retryable=False):
        state["stop_reason"] = dict(code=code, message=message)
        output = self._diagnostic(request, state, message=message, outcome="failed",
                                  error=error_for(request["run_id"], code, message, retryable=retryable))
        return worker.commit_step(job["id"], output, status="failed", quality="blocked", graph_state=state)

    def step(self, worker, run_id):
        run = self.storage.get_run(run_id)
        request, spec = self._request(run)
        state = self._state(run)
        job = worker.begin_step(run_id, expected_revision=run["revision"])
        if state["registry"] != self.registry.manifest():
            return self._failed(worker, job, request, state, "INVALID_STATE", "Конфигурация этапов изменилась после контрольной точки.")
        context = StageContext(inputs=self.storage.list_inputs(run_id), feedback=deepcopy(state["feedback"]),
                               repairs_used=state["repairs_used"], synthetic=self.registry.synthetic)
        try:
            output = spec.handler(deepcopy(request), deepcopy(context))
        except ModelError as error:
            if error.code in ("INVALID_RESPONSE", "INCOMPLETE_RESPONSE"):
                return self._invalid(worker, job, request, state)
            return self._failed(worker, job, request, state, "UPSTREAM_UNAVAILABLE", "Вызов модели не выполнен.", retryable=error.retryable)
        except TimeoutError:
            return self._failed(worker, job, request, state, "UPSTREAM_UNAVAILABLE", "Время ожидания этапа истекло.", retryable=True)
        except Exception:
            return self._failed(worker, job, request, state, "INTERNAL_ERROR", "Обработчик этапа завершился с ошибкой.")
        try:
            # Copy to plain finite JSON before schema checks or passing data to another stage.
            output = json.loads(encode(output))
            self._check_output(request, output)
            route = self._route(request, output, state)
            if state["stop_reason"]:
                output["artifacts"].extend(self._diagnostic(request, state, message=state["stop_reason"]["message"])["artifacts"])
            return worker.commit_step(job["id"], output, graph_state=state, **route)
        except (StorageError, TypeError, ValueError, RecursionError):
            # Routing changes are discarded if result validation/atomic persistence failed.
            # SQLite/OSError failures deliberately propagate: they must not trigger another model call.
            return self._invalid(worker, job, request, self._state(run))

    def execute(self, worker, run_id, stop):
        worker.claim_run(run_id)
        while self.storage.get_run(run_id)["status"] == "running":
            if stop.is_set():
                return worker.pause_at_boundary(run_id)
            self.step(worker, run_id)
        return self.storage.get_run(run_id)
