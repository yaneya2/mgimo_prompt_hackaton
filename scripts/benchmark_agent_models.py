"""Short opt-in model comparison on the same extraction/alternatives/criticism tasks."""
import argparse
import json
from pathlib import Path
from time import monotonic
from uuid import uuid4

from app.config import Settings
from app.llm import create_model
from app.llm.base import ModelError
from app.llm.usage import JsonlUsageSink

TASKS = {
    'extraction': 'Из брифа извлеки только доступные функции: есть email-напоминания, онлайн-оплата планируется, интеграции с мессенджерами нет. Не добавляй вымышленных функций.',
    'alternatives': 'Для нового сервиса ручного учёта заявок мастерской предложи три направления, отличающиеся сегментом или выходом на рынок. Бюджет 50000 RUB, горизонт 4 недели, платная реклама запрещена. Обозначь допущения.',
    'criticism': 'Найди противоречие. Карточка продукта: есть только email-напоминания. Рекламный текст: напомним в Telegram и гарантируем снижение пропусков на 50%. Статистики и интеграции с Telegram нет. Укажи исправления.',
}
SCHEMA = dict(type='object', additionalProperties=False, required=['answer'], properties={'answer': {'type': 'string', 'minLength': 1}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', action='append', required=True, help='Exact model ID; repeat for a second available model')
    parser.add_argument('--output', type=Path, default=Path('data/model-benchmark'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    base = Settings()
    sink = JsonlUsageSink(args.output / 'usage.jsonl')
    for model_id in args.model:
        settings = base.model_copy(update={'model_name': model_id})
        for task, prompt in TASKS.items():
            started = monotonic()
            try:
                with create_model(settings, sink) as model:
                    result = model.generate(messages=[{'role': 'system', 'content': 'Отвечай по-русски. Не выдумывай данные. Верни JSON.'},
                                                      {'role': 'user', 'content': prompt}], schema=SCHEMA,
                                            run_id='benchmark_' + uuid4().hex, stage='review' if task == 'criticism' else 'brief',
                                            prompt_version='model-probe-v1', schema_version='model-probe-v1')
                records.append(dict(model_requested=model_id, model_actual=result.model, task=task, outcome='success',
                                    duration_seconds=monotonic()-started, answer=result.data['answer'], usage_ids=result.usage_ids))
            except ModelError as exc:
                records.append(dict(model_requested=model_id, task=task, outcome='error', error_code=exc.code,
                                    duration_seconds=monotonic()-started, usage_ids=exc.usage_ids))
    destination = args.output / ('benchmark_' + uuid4().hex + '.json')
    destination.write_text(json.dumps(dict(records=records, human_scoring_required=True,
                                         rubric='0–2: точность извлечения; различия альтернатив; обнаружение противоречий; соблюдение ограничений'),
                                      ensure_ascii=False, indent=2) + '\n')
    print(destination)


if __name__ == '__main__':
    main()
