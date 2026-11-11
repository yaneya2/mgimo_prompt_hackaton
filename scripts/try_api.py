"""Synthetic A6 acceptance through HTTP only; run the API and demo worker separately."""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx


def run_demo(base_url, output_dir):
    with httpx.Client(base_url=base_url.rstrip('/'), timeout=10, trust_env=False) as client:
        def read(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        def post(path, body, key=None):
            response = client.post('/api/v1' + path, json=body, headers={'Idempotency-Key': key or uuid4().hex})
            response.raise_for_status()
            return response.json()

        brief = dict(product='Синтетический сервис расписания', goal='Проверить HTTP-сценарий',
                     audience=['Преподаватели'], horizon='Шесть недель', budget=None,
                     constraints=['Все результаты синтетические'], assumptions=[], unknowns=['Бюджет'], excluded_source_ids=[])
        project = post('/projects', dict(name='Синтетическая проверка A6', brief=brief))
        limits = dict(max_model_calls=20, max_search_calls=10, max_repair_cycles=2, timeout_seconds=120, budget=None)
        body, key = dict(brief_version=1, limits=limits), uuid4().hex
        run = post('/projects/' + project['id'] + '/runs', body, key)
        if post('/projects/' + project['id'] + '/runs', body, key) != run:
            raise RuntimeError('Repeated command returned a different run')
        path = '/runs/' + run['id']

        def wait(status, stage):
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                snapshot = read('/api/v1' + path + '/snapshot')
                current = snapshot['run']
                if (current['status'], current['stage']) == (status, stage):
                    return snapshot
                if current['status'] in ('paused', 'failed', 'incomplete', 'completed', 'waiting_for_input'):
                    raise RuntimeError(f"Unexpected demo state: {current['status']}/{current['stage']}")
                time.sleep(0.1)
            raise TimeoutError('Demo worker did not reach the expected state within 40 seconds')

        waiting = wait('waiting_for_input', 'brief')
        # Answers and selection are simulated only in this explicitly synthetic check.
        post(path + '/answers', dict(expected_revision=waiting['run']['revision'],
             answers=[dict(question_id=q['id'], value='Тестовый бюджет: 100000 RUB') for q in waiting['questions']]))
        waiting = wait('waiting_for_input', 'selection')
        if waiting['data']['decisions']:
            raise RuntimeError('A decision appeared before human selection')
        option = waiting['data']['options'][0]
        post(path + '/selection', dict(expected_revision=waiting['run']['revision'], option_id=option['id'],
             option_version=option['version'], rationale='Синтетический выбор для проверки API'))
        snapshot = wait('completed', 'report')
        if snapshot['run']['quality'] != 'draft' or snapshot['usage']:
            raise RuntimeError('Use this check with the synthetic demo worker')
        output_dir = Path(output_dir).resolve() / run['id']
        output_dir.mkdir(parents=True, exist_ok=False)
        (output_dir / 'snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        exports = []
        for format, extension in [('markdown', 'md'), ('html', 'html')]:
            exported = post('/projects/' + project['id'] + '/exports', dict(run_id=run['id'],
                            expected_revision=snapshot['run']['revision'], format=format, allow_draft=True))
            response = client.get(exported['download_url'])
            response.raise_for_status()
            if 'ЧЕРНОВИК' not in response.text:
                raise RuntimeError('Synthetic export is not marked as a draft')
            (output_dir / ('report.' + extension)).write_bytes(response.content)
            exports.append(exported)
        result = dict(synthetic=True, project_id=project['id'], run_id=run['id'], status='completed', quality='draft',
                      usage_events=0, output_dir=str(output_dir), exports=exports)
        (output_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--output-dir', type=Path, default=Path('data/api-checks'))
    args = parser.parse_args()
    print(json.dumps(run_demo(args.base_url, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
