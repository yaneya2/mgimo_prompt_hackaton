"""Assemble the selected strategy exclusively from saved structured data."""
import json

from app.agents.contracts import ref
from app.workflow.results import artifact_for, result_for


def run(request, context):
    data = request['input_data']
    selected = next(d for d in reversed(data['decisions']) if d['author'] == 'user' and d['action'] == 'select')
    option = next(o for o in data['options'] if o['id'] == selected['option_id'])
    brief = request['brief']['input']
    lines = ['# Стратегия: ' + brief['product'], '', 'Цель: ' + brief['goal'],
             'Горизонт: ' + brief['horizon'], 'Бюджет: ' + json.dumps(brief['budget'], ensure_ascii=False),
             '', '## Решение руководителя', option['name'], selected['rationale'],
             'Сегмент: ' + option['segment'], 'Проблема: ' + option['problem'], 'Ценность: ' + option['value'],
             'Приоритеты: ' + '; '.join(option['priorities']), 'Основания: ' + option['rationale'],
             'Ресурсы и допущения: ' + '; '.join(option['resource_assumptions']),
             'Возражения: ' + '; '.join(option['objections']), 'Пересмотреть решение, если: ' + option['failure_condition'],
             '', '## Ограничения и неизвестные', *brief['constraints'], *brief['assumptions'], *brief['unknowns'],
             '', '## Альтернативы']
    for item in data['options']:
        lines += ['- ' + item['name'] + ': ' + item['segment'] + '; ' + item['value'] + '. ' + item['rationale']]
    lines += ['', '## Возможности продукта']
    caps = {c['id']: c for c in data['capabilities']}
    for cap in caps.values():
        lines += ['- ' + cap['name'] + ' [' + cap['status'] + ']: ' + cap['basis']]
    lines += ['', '## Матрица обещаний и проверок']
    for message in data['messages']:
        if message['option_id'] != option['id']:
            continue
        lines += ['- ' + message['text'] + ' [' + message['status'] + ']; канал: ' + message['channel'],
                  '  Возможности: ' + ', '.join(caps[c]['name'] for c in message['capability_ids']),
                  '  Основания: ' + ', '.join(message['claim_ids']) + '; ' + message['rationale']]
        for exp in data['experiments']:
            if message['id'] in exp['message_ids']:
                lines += ['  Проверка: ' + exp['hypothesis'] + '; ' + exp['method'],
                          '  Метрика: ' + exp['metric'] + '; исходное значение: ' + (exp['baseline'] or 'требуется первичное измерение'),
                          '  Порог: ' + exp['decision_threshold'] + '; основание порога: ' + exp['threshold_basis'],
                          '  Срок: ' + exp['duration'] + '; ответственный: ' + (exp['owner'] or 'роль не назначена'),
                          '  Данные: ' + '; '.join(exp['data_requirements']) + '; остановка: ' + exp['stop_condition']]
    lines += ['', '## Утверждения и доказательства']
    for claim in data['claims']:
        lines += ['- ' + claim['id'] + ': ' + claim['text'] + ' [' + claim['kind'] + '/' + claim['verification'] + ']',
                  '  Ограничения: ' + '; '.join(claim['limitations'])]
        for evidence in claim['evidence']:
            lines += ['  ' + evidence['relation'] + ': «' + evidence['quote'] + '» — ' + evidence['source_id'] + ', ' + evidence['locator']]
    lines += ['', '## Источники']
    for source in data['sources']:
        lines += ['- ' + source['id'] + ': ' + source['title'] + ' ' + (source['url'] or '') + ' [' + source['verification'] + ']',
                  '  Получен: ' + source['retrieved_at'] + '; ' + source['origin_note']]
    lines += ['', '## Замечания критика']
    lines += [i['code'] + ' [' + i['status'] + ']: ' + i['explanation'] for i in data['issues']]
    lines += ['', 'Стоимость и фактические модели находятся в журнале Usage платформы. Неизвестная стоимость не равна нулю.',
              'Сохранённые цитаты подтверждают происхождение, а смысловую поддержку нужно проверять. Реальные интервью не проводились.']
    if context.synthetic:
        lines.insert(0, 'СИНТЕТИЧЕСКАЯ ПРОВЕРКА: ответы модели заменены fixtures, качество стратегии не оценивалось.\n')
    output = result_for(request)
    output['artifacts'] = [artifact_for(request, '\n'.join(lines), entities=[ref('options', option), ref('decisions', selected)])]
    return output
