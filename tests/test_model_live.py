"""Real generation tests; skipped unless --run-live-model is supplied."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.config import Settings
from app.llm import create_model, JsonlUsageSink, ModelError

pytestmark = pytest.mark.live_model


@pytest.fixture
def generate():
    settings = Settings()
    folder = settings.data_dir / "model-checks"
    folder.mkdir(parents=True, exist_ok=True)

    def call(name, prompt, schema):
        run_id = "run_live_" + uuid4().hex
        usage_file = folder / f"{run_id}.usage.jsonl"
        report_file = folder / f"{run_id}.json"
        report = {"test": name, "run_id": run_id,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "provider": settings.model_provider, "prompt": prompt}
        try:
            with create_model(settings, JsonlUsageSink(usage_file)) as model:
                result = model.generate(
                    messages=[{"role": "user", "content": prompt}],
                    schema=schema, run_id=run_id, stage="brief")
        except ModelError as error:
            report.update(status="model_error", error_code=error.code,
                          error_message=str(error), usage_ids=error.usage_ids)
            report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            pytest.fail(f"{error.code}: {error}; report: {report_file}", pytrace=False)
        report.update(status="generated", data=result.data, model=result.model,
                      generation_id=result.generation_id, usage_ids=result.usage_ids)
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
        # Check that an actual completed adapter call was accounted for.
        records = [json.loads(line) for line in usage_file.read_text().splitlines()]
        assert result.usage_ids
        assert result.usage_ids == [event["id"] for event in records]
        assert records[-1]["outcome"] == "success"
        assert all(event["run_id"] == run_id for event in records)
        assert all(event["provider"] == settings.model_provider for event in records)
        assert "data" in report
        return result.data

    return call


def test_generate_russian_tagline(generate):
    data = generate(
        "russian_tagline",
        "Придумай короткий слоган на русском языке для вымышленного сервиса "
        "расписания занятий для частных преподавателей. Он умеет отправлять "
        "напоминания по email. Не обещай измеримых результатов, интеграций "
        "или функций, которых нет в описании. Верни JSON с полем tagline, "
        "не длиннее 120 символов.",
        {"type": "object", "properties": {"tagline": {"type": "string"}},
         "required": ["tagline"], "additionalProperties": False},
    )
    assert 5 <= len(data["tagline"].strip()) <= 120
    assert any("а" <= c.lower() <= "я" or c.lower() == "ё" for c in data["tagline"])


def test_generate_three_interview_questions(generate):
    data = generate(
        "interview_questions",
        "Составь ровно три разных открытых вопроса на русском языке для интервью "
        "с частным преподавателем о том, как он ведёт расписание и напоминает "
        "ученикам о занятиях. Спрашивай о реальном прошлом опыте, избегай рекламы "
        "и предположений, что ему нужен наш продукт. Верни JSON: поле questions "
        "со списком из трёх строк, каждая не длиннее 300 символов.",
        {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
         "required": ["questions"], "additionalProperties": False},
    )
    questions = data["questions"]
    assert len(questions) == 3
    assert len({q.strip().casefold() for q in questions}) == 3
    assert all(10 <= len(q.strip()) <= 300 for q in questions)
    assert all(any("а" <= c.lower() <= "я" or c.lower() == "ё" for c in q) for q in questions)
