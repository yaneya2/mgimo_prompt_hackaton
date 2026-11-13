"""Evaluate strategies on frozen evidence using production handlers and bounded A5 routing."""
import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import time
from uuid import uuid4

from app.agents.contracts import META, response_schema
from app.agents.checks import check_bundle
from app.agents.context import effective_context
from app.agents.runtime import AgentRuntime
from app.config import Settings
from app.llm.base import ModelResult
from app.llm.usage import JsonlUsageSink
from app.storage import Storage
from app.storage.validation import empty_data
from app.worker import WorkerService
from app.workflow.results import artifact_for, result_for
from app.workflow.stages import AGENT_STAGES, StageRegistry, StageSpec

MODES = ('single', 'no-critic', 'critic-once', 'full')


class EvaluationSink:
    def __init__(self, store, path):
        self.store, self.journal, self.events = store, JsonlUsageSink(path), []
    def record(self, event):
        self.store.record(event)
        self.journal.record(event)
        self.events.append(deepcopy(event))


def frozen_data(snapshot, project_id):
    """Isolated evaluation stores start at version 1; preserve the original evidence in input.json."""
    data = empty_data()
    for collection in ('sources', 'capabilities', 'claims'):
        for item in snapshot['data'][collection]:
            item = deepcopy(item)
            item.update(project_id=project_id, brief_version=1, version=1)
            for evidence in item.get('evidence', []):
                evidence['source_version'] = 1
            data[collection].append(item)
    return data


def evaluate(snapshot, settings, directory, mode, *, inputs=(), model_factory=None):
    directory.mkdir(parents=True, exist_ok=True)
    store = Storage(directory / 'state')
    project = store.create_project('Оценка ' + mode, deepcopy(snapshot['brief']['input']))
    run = store.create_run(project['id'], dict(max_model_calls=30, max_search_calls=1,
                            max_repair_cycles=0 if mode == 'critic-once' else 2, timeout_seconds=1800, budget=None))
    sink = EvaluationSink(store, directory / 'usage.jsonl')
    runtime = AgentRuntime(settings, sink, **({'model_factory': model_factory} if model_factory else {}))
    base = frozen_data(snapshot, project['id'])
    answers = deepcopy(list(inputs))
    for answer in answers:
        # Fresh evaluation BriefVersion 1 represents the saved input version exactly, with provenance kept in input.json.
        if answer.get('brief_version') == snapshot['brief']['version']:
            answer['brief_version'] = 1
        else:
            answer['brief_version'] = -1
    pending = {}
    research_calls = 0

    def seeded(req, collections, summary):
        output = result_for(req)
        for key in collections:
            output['data'][key] = deepcopy(base[key])
        output['artifacts'] = [artifact_for(req, summary)]
        return output

    def handler(req, context):
        nonlocal research_calls
        context = replace(context, inputs=answers + context.inputs)
        stage = req['stage']
        if stage == 'brief':
            return seeded(req, ('sources', 'capabilities'), 'Замороженный бриф и возможности: одинаковые исходные материалы оценки.')
        if stage == 'research':
            research_calls += 1
            if research_calls == 1:
                return seeded(req, ('claims',), 'Прочитанные источники и утверждения из сохранённого снимка.')
            # A critique may re-evaluate claims, but may not obtain extra evidence unavailable to the other modes.
            runtime.gate.begin(req)
            try:
                return runtime.execute(req, context, frozen_research=True)
            finally:
                runtime.gate.finish(req)
        if stage == 'review' and mode in ('single', 'no-critic'):
            output = result_for(req)
            output['artifacts'] = [artifact_for(req, 'Критик отключён только в оценочной абляции. Выбор остаётся за пользователем.')]
            return output
        if mode == 'single' and stage == 'options':
            schema = response_schema('options')
            comm = response_schema('communications')
            for key in ('messages', 'experiments'):
                schema['properties'][key] = comm['properties'][key]
                schema['required'].append(key)
            schema['$defs'].update(comm['$defs'])
            runtime.gate.begin(req)
            try:
                generated = runtime.generate(req, context, stage, schema=schema,
                    instruction='Одновызовный базовый режим. Подготовь одновременно три options, messages и experiments; '
                                'свяжи сообщения с возможностями и измеримыми проверками. Отдельный критик не вызывается.')
            finally:
                runtime.gate.finish(req)
            pending['communications'] = ModelResult({key: generated.data[key] for key in ('summary', 'questions', 'messages', 'experiments')},
                                                     generated.model, generated.generation_id, [])
            generated = ModelResult({key: generated.data[key] for key in ('summary', 'questions', 'options')},
                                    generated.model, generated.generation_id, generated.usage_ids)
            return runtime.execute(req, context, generated=generated)
        if mode == 'single' and stage == 'communications':
            return runtime.execute(req, context, generated=pending['communications'])
        return runtime(req, context)

    registry = StageRegistry('evaluation-' + mode + '-v2', {stage: StageSpec(handler, stage + '-v2', settings.model_name)
                                                           for stage in AGENT_STAGES})
    started = time.monotonic()
    WorkerService(store, registry).run(once=True)
    final = store.get_snapshot(run['id'])
    found = {}
    for job in store.list_jobs(run['id']):
        if job['checkpoint_id']:
            for issue in store.get_checkpoint(job['checkpoint_id'])['data']['issues']:
                if issue['status'] != 'resolved':
                    found[issue['id']] = issue
    residual = [i for i in final['data']['issues'] if i['status'] != 'resolved']
    fixed = [i for i in final['data']['issues'] if i['id'] in found and i['status'] == 'resolved']
    defects = check_bundle(final['data'], final['brief']['input']['excluded_source_ids'])
    events = sink.events
    result = dict(mode=mode, evaluation_only=True, independent_acceptance=False,
                  source_brief_version=snapshot['brief']['version'], run_id=run['id'], status=final['run']['status'],
                  stage=final['run']['stage'], comparison_complete=final['run']['stage']=='selection' and bool(final['data']['options']) and bool(final['data']['messages']) and bool(final['data']['experiments']),
                  effective_context=effective_context(final['brief'], answers), data=final['data'],
                  summaries=[a['summary'] for a in final['artifacts'] if a['freshness'] == 'current'],
                  defects_found=list(found.values()), defects_fixed=fixed, residual_issues=residual,
                  deterministic_defects=defects, duration_seconds=time.monotonic() - started,
                  model_requested=settings.model_name, models_reported=list(dict.fromkeys(e['model'] for e in events)),
                  parameters=dict(provider=settings.model_provider, max_tokens=settings.model_max_tokens,
                                  max_retries=settings.model_max_retries, timeout_seconds=settings.model_timeout_seconds),
                  usage_ids=[e['id'] for e in events], calls=len({e['call_id'] for e in events if e['tool'] in (None, 'fixture_generate')}),
                  cost_events=[dict(cost=e['cost'], cost_basis=e['cost_basis']) for e in events],
                  human_selection_submitted=False)
    # The evaluation stops at the actual A5 selection boundary; no fabricated human choice or final report.
    (directory / 'snapshot.json').write_text(json.dumps(final, ensure_ascii=False, indent=2) + '\n')
    path = directory / 'result.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return dict(mode=mode, file=str(path), calls=result['calls'], defects=len(defects),
                found=len(found), fixed=len(fixed), residual=len(residual), status=result['status'])


def compare(snapshot, settings, output_dir, modes=('single', 'no-critic', 'full'), *, inputs=(), model_factory=None):
    if not settings.model_name:
        raise ValueError('Для повторяемого сравнения задайте точный APP_MODEL_NAME.')
    if any(mode not in MODES for mode in modes):
        raise ValueError('Неизвестный режим сравнения')
    output_dir.mkdir(parents=True, exist_ok=True)
    # All modes receive exactly the same frozen evidence and effective clarifications.
    (output_dir / 'input.json').write_text(json.dumps(dict(snapshot=snapshot, inputs=list(inputs)), ensure_ascii=False, indent=2) + '\n')
    batch = uuid4().hex[:12]
    return [evaluate(snapshot, settings, output_dir / (batch + '-' + mode), mode, inputs=inputs,
                     model_factory=model_factory) for mode in modes]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, help='Saved run inputs, including original questions and clarifications')
    parser.add_argument('--output', type=Path, default=Path('data/comparisons'))
    parser.add_argument('--mode', choices=MODES, action='append')
    args = parser.parse_args()
    print(json.dumps(compare(json.loads(args.snapshot.read_text()), Settings(), args.output,
                             args.mode or ('single', 'no-critic', 'full'),
                             inputs=json.loads(args.inputs.read_text()) if args.inputs else ()), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
