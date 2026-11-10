"""Synthetic end-to-end A5 check using three fresh worker processes and saved user input."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from app.storage import Storage

LIMITS = dict(max_model_calls=20, max_search_calls=10, max_repair_cycles=2, timeout_seconds=120, budget=None)


def demo_brief(product="Условный сервис расписания для преподавателей"):
    return dict(product=product, goal="Проверить путь от брифа до тестового отчёта", audience=["Частные преподаватели"],
                horizon="Шесть недель", budget=None, constraints=["Все результаты синтетические"], assumptions=[],
                unknowns=["Бюджет"], excluded_source_ids=[])


def run_demo(data_dir, *, product=None):
    storage = Storage(data_dir)
    project = storage.create_project("Синтетическая проверка A5", demo_brief(product) if product else demo_brief())
    run = storage.create_run(project["id"], LIMITS)
    root = Path(__file__).resolve().parents[1]

    def execute():
        subprocess.run([sys.executable, "-E", "-m", "app.worker", "--stages", "demo", "--once",
                        "--data-dir", str(storage.data_dir)], cwd=root, check=True, timeout=30)
        return storage.get_snapshot(run["id"])

    snapshot = execute()
    if snapshot["run"]["status"] != "waiting_for_input" or snapshot["run"]["stage"] != "brief":
        raise RuntimeError("Demo did not stop for brief clarification")
    # Only this explicitly synthetic check simulates the human's answers and selection.
    storage.submit_answers(run["id"], [{"question_id": "question_budget", "value": "Тестовый бюджет: 100000 RUB"}],
                           expected_revision=snapshot["run"]["revision"])
    snapshot = execute()
    if snapshot["run"]["status"] != "waiting_for_input" or snapshot["run"]["stage"] != "selection":
        raise RuntimeError("Demo did not stop for human selection")
    option = snapshot["data"]["options"][0]
    storage.submit_selection(run["id"], option["id"], option["version"], "Синтетический выбор для проверки платформы",
                             expected_revision=snapshot["run"]["revision"])
    snapshot = execute()
    if snapshot["run"]["status"] != "completed" or snapshot["run"]["quality"] != "draft":
        raise RuntimeError("Synthetic workflow did not complete as a draft")
    destination = storage.data_dir / (run["id"] + ".snapshot.json")
    destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dict(synthetic=True, run_id=run["id"], status=snapshot["run"]["status"], quality=snapshot["run"]["quality"],
                jobs=len(storage.list_jobs(run["id"])), usage_events=len(snapshot["usage"]), snapshot=str(destination))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/workflow-checks"))
    parser.add_argument("--product")
    args = parser.parse_args()
    print(json.dumps(run_demo(args.data_dir, product=args.product), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
