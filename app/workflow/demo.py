"""Explicitly synthetic stages for integration checks. No model or search calls."""
from copy import deepcopy
import hashlib

from app.storage.database import now
from app.storage.validation import COLLECTIONS
from app.workflow.results import artifact_for, result_for
from app.workflow.stages import AGENT_STAGES, StageRegistry, StageSpec


def identity(request, label):
    return label + "_" + hashlib.sha256((request["run_id"] + label).encode()).hexdigest()[:20]


def entity(request, entity_kind, label, **fields):
    entity_id = identity(request, label)
    previous = next((item for item in request["input_data"][COLLECTIONS[entity_kind]] if item["id"] == entity_id), None)
    return dict(id=entity_id, version=previous["version"] + 1 if previous else 1,
                project_id=request["project_id"], brief_version=request["brief"]["version"], **fields)


def ref(kind, value):
    return dict(kind=kind, id=value["id"], version=value["version"])


def demo_stage(request, context):
    output = result_for(request)
    data = output["data"]
    inputs = request["input_data"]
    stage = request["stage"]
    product = request["brief"]["input"]["product"]
    targets = []
    summary = "СИНТЕТИЧЕСКИЙ ТЕСТ. "
    if stage == "brief":
        answers = [answer for item in context.inputs if item["kind"] == "answers" for answer in item["payload"]["answers"]]
        budget_answer = next((answer["value"] for answer in reversed(answers) if answer["question_id"] == "question_budget"), None)
        if request["brief"]["input"]["budget"] is None and budget_answer is None:
            return result_for(request, outcome="needs_input", questions=[dict(
                id="question_budget", stage="brief", text="Каков бюджет? Можно явно указать, что он пока неизвестен.", required=True, options=[])])
        excerpt = "Тестовый продукт: " + product + ". Цель: " + request["brief"]["input"]["goal"]
        if budget_answer:
            excerpt += ". Ответ о бюджете: " + budget_answer
        source = entity(request, "source", "source_brief", kind="user_text", title="Тестовый бриф", url=None,
                        publisher=None, published_at=None, retrieved_at=now(), content_excerpt=excerpt, locator="Бриф пользователя",
                        verification="read", origin_note="Синтетический прогон A5; сведения пользователя не проверены независимо")
        data["sources"] = [source]
        targets = [ref("source", source)]
        summary += "Бриф сохранён: " + product
    elif stage == "research":
        claim = entity(request, "claim", "claim_demand", text="Есть ли спрос на продукт «" + product + "»?",
                       kind="hypothesis", verification="unverified", evidence=[],
                       limitations=["Тестовая гипотеза. Поиск и проверка внешних источников не проводились."])
        data["claims"] = [claim]
        targets = [ref("claim", claim)]
        summary += "Исследование заменено тестовой гипотезой; внешних подтверждений нет."
    elif stage == "options":
        for number, channel in enumerate(("Интервью", "Ручной пилот", "Партнёрский тест"), 1):
            option = entity(request, "option", "option_" + str(number), name="Тестовое направление " + str(number),
                            segment="Тестовый сегмент " + str(number), problem="Проверить потребность в «" + product + "»",
                            value=channel + " для проверки спроса", priorities=["Собрать обратную связь"], capability_ids=[],
                            claim_ids=[claim["id"] for claim in inputs["claims"]], channels=[channel],
                            resource_assumptions=["Ресурсы требуют уточнения"], objections=["Спрос может не подтвердиться"],
                            failure_condition="Участники не возвращаются", rationale="Синтетическая альтернатива для проверки маршрута")
            data["options"].append(option)
            targets.append(ref("option", option))
        summary += "Сохранены три тестовых направления."
    elif stage == "communications":
        option = inputs["options"][0]
        message = entity(request, "message", "message_test", option_id=option["id"], text="Обсудим вашу потребность в «" + product + "».",
                         capability_ids=[], claim_ids=[item["id"] for item in inputs["claims"]], channel="Интервью",
                         status="hypothesis_only", rationale="Тестовый текст; обещания доступных функций отсутствуют")
        experiment = entity(request, "experiment", "experiment_test", option_id=option["id"], message_ids=[message["id"]],
                            hypothesis="Выбранный сегмент заинтересован в пилоте", method="Пять интервью и приглашение в пилот",
                            metric="Количество согласившихся на пилот", baseline=None, decision_threshold="Не менее 3 из 5",
                            threshold_basis="Синтетическое допущение, не статистический вывод", duration="Две недели", owner=None,
                            data_requirements=["Согласие участников"], stop_condition="Нет доступных участников")
        data["messages"], data["experiments"] = [message], [experiment]
        targets = [ref("message", message), ref("experiment", experiment)]
        summary += "Сохранены тестовые сообщение и эксперимент."
    elif stage == "review":
        summary += "Тестовый критик завершён; содержательная проверка реальными агентами не выполнялась."
        targets = [ref(kind, item) for kind in ("claim", "option", "message", "experiment", "issue")
                   for item in inputs[COLLECTIONS[kind]]]
    elif stage == "report":
        selected = next(item for item in reversed(inputs["decisions"]) if item["author"] == "user" and item["action"] == "select")
        option = next(item for item in inputs["options"] if item["id"] == selected["option_id"])
        targets = [ref("option", option), ref("decision", selected)]
        summary += "Продукт: " + product + ". Выбрано: " + option["name"] + ". Проверка: интервью и пилот. Это проверка платформы, не готовая стратегия."
    # Synthetic repair fixture: mark an issue handled only when its designated stage executes.
    versions = {(kind, item["id"]): item["version"] for kind, collection in COLLECTIONS.items() for item in data[collection]}
    for issue in inputs["issues"]:
        if issue["return_to"] == stage and issue["status"] != "resolved":
            updated = deepcopy(issue)
            updated.update(version=issue["version"] + 1, status="resolved")
            target = updated["target"]
            target["version"] = versions.get((target["kind"], target["id"]), target["version"])
            data["issues"].append(updated)
            # The issue ledger is persisted in DataBundle. Re-review may reopen an
            # issue without invalidating the newly produced communication itself.
    output["artifacts"] = [artifact_for(request, summary, entities=targets)]
    return output


def build_stages(settings=None, sink=None):
    return StageRegistry("synthetic-demo-v1", {stage: StageSpec(demo_stage, "synthetic-" + stage + "-v1", "synthetic-no-model")
                                               for stage in AGENT_STAGES}, synthetic=True)
