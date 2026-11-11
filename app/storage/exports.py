"""Current export context and a local fallback renderer for the C6 integration seam."""
from copy import deepcopy
import html
import json
import re

from app.storage.validation import COLLECTIONS


def current_snapshot(snapshot):
    snapshot = deepcopy(snapshot)
    version = snapshot["run"]["brief_version"]
    snapshot["artifacts"] = [a for a in snapshot["artifacts"] if a["freshness"] == "current" and a["brief_version"] == version]
    indexes = {kind: {item["id"]: item for item in snapshot["data"][collection] if item["brief_version"] == version}
               for kind, collection in COLLECTIONS.items()}
    pending = [(ref["kind"], ref["id"]) for a in snapshot["artifacts"] for ref in a["entities"]]
    # Pending review issues remain relevant while their targets are being repaired.
    pending += [("issue", item["id"]) for item in indexes["issue"].values()]
    retained = set()
    while pending:
        kind, identity = pending.pop()
        item = indexes[kind].get(identity)
        if item is None or (kind, identity) in retained:
            continue
        retained.add((kind, identity))
        for field, target in (("source_ids", "source"), ("capability_ids", "capability"),
                              ("claim_ids", "claim"), ("message_ids", "message")):
            pending += [(target, value) for value in item.get(field, [])]
        if "option_id" in item:
            pending.append(("option", item["option_id"]))
        if "target" in item:
            pending.append((item["target"]["kind"], item["target"]["id"]))
        pending += [("source", e["source_id"]) for e in item.get("evidence", [])]
    for kind, collection in COLLECTIONS.items():
        snapshot["data"][collection] = [item for identity, item in indexes[kind].items() if (kind, identity) in retained]
    return snapshot


def render_snapshot(snapshot, format):
    """C6 may replace this callable: (validated current snapshot, format) -> UTF-8 text."""
    run = snapshot["run"]
    labels = {"draft": "ЧЕРНОВИК", "blocked": "ЧЕРНОВИК — ЕСТЬ БЛОКИРУЮЩИЕ ЗАМЕЧАНИЯ", "ready": "ГОТОВЫЙ РЕЗУЛЬТАТ"}
    lines = ["# " + snapshot["project"]["name"], "", labels[run["quality"]], "",
             f"Запуск: {run['id']}; ревизия: {run['revision']}; версия брифа: {run['brief_version']}.",
             "Это сохранённая версия документа; последующие изменения проекта в неё не входят.", ""]
    reports = [a for a in snapshot["artifacts"] if a["stage"] == "report"]
    if reports:
        lines += [reports[-1]["summary"], ""]
    else:
        lines += ["Итоговый отчёт ещё не подготовлен. Ниже — сохранённые результаты текущих этапов.", ""]
        for artifact in snapshot["artifacts"]:
            lines += ["## " + artifact["stage"], "", artifact["summary"], ""]
    # Exact structured data is available even before the final C6 renderer is connected.
    data = json.dumps(dict(brief=snapshot["brief"], data=snapshot["data"], questions=snapshot["questions"], usage=snapshot["usage"]),
                      ensure_ascii=False, indent=2)
    fence = "`" * max(3, max((len(value) + 1 for value in re.findall(r"`+", data)), default=3))
    lines += ["## Данные и основания", "", fence + "json", data, fence]
    markdown = "\n".join(lines) + "\n"
    if format == "markdown":
        return markdown
    if format != "html":
        raise ValueError("Unsupported export format")
    return '<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>' + html.escape(snapshot["project"]["name"]) + \
           '</title></head><body><pre>' + html.escape(markdown) + '</pre></body></html>\n'
