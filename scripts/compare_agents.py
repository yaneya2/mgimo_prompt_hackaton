"""Offline comparison on identical saved brief/evidence; never bypasses production human gates."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from app.agents.contracts import response_schema, stamp
from app.agents.checks import check_bundle
from app.agents.runtime import AgentRuntime
from app.config import Settings
from app.llm.usage import JsonlUsageSink
from app.workflow.stages import StageContext


def compare(snapshot, settings, output_dir, modes=('single', 'no-critic', 'full')):
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    base = deepcopy(snapshot['data'])
    for key in ('options', 'messages', 'experiments', 'decisions', 'issues'):
        base[key] = []
    for mode in modes:
        sink = JsonlUsageSink(output_dir / (mode + '.usage.jsonl'))
        runtime = AgentRuntime(settings, sink)
        from uuid import uuid4
        req = dict(schema_version='1.0.0', run_id='eval_' + uuid4().hex, project_id=snapshot['project']['id'],
                   stage='options', brief=snapshot['brief'], input_data=deepcopy(base), input_artifacts=[],
                   limits=dict(max_model_calls=20, max_search_calls=1, max_repair_cycles=0, timeout_seconds=1800, budget=None),
                   prompt_version='comparison-' + mode + '-v1', model_id=settings.model_name or 'provider-default')
        context = StageContext([], [], 0, False)
        summaries = []
        usage = []
        stages = ['options'] if mode == 'single' else ['options', 'communications'] + (['review'] if mode == 'full' else [])
        for stage in stages:
            req['stage'] = stage
            schema = response_schema(stage)
            instruction = None
            if mode == 'single':
                comm = response_schema('communications')
                for key in ('messages', 'experiments'):
                    schema['properties'][key] = comm['properties'][key]
                    schema['required'].append(key)
                instruction = ('Одновызовный базовый режим. Одновременно подготовь три options, messages и experiments '
                               'на том же брифе и источниках. Соблюдай матрицу возможностей и проверок, '
                               'порог, основание порога, срок и ответственную роль. Не выполняй отдельного критика.')
            generated = runtime.generate(req, context, stage, schema=schema, instruction=instruction)
            usage += generated.usage_ids
            summaries.append(generated.data['summary'])
            for collection in ('options', 'messages', 'experiments', 'issues'):
                if collection in generated.data:
                    req['input_data'][collection] = [stamp(req, collection, i) for i in generated.data[collection]]
        result = dict(mode=mode, evaluation_only=True, independent_acceptance=False, brief=snapshot['brief'],
                      data=req['input_data'], summaries=summaries, usage_ids=usage,
                      deterministic_defects=check_bundle(req['input_data'], snapshot['brief']['input']['excluded_source_ids']))
        path = output_dir / (mode + '.json')
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        results.append({'mode': mode, 'file': str(path), 'calls': len(stages), 'defects': len(result['deterministic_defects'])})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('data/comparisons'))
    parser.add_argument('--mode', choices=['single', 'no-critic', 'full'], action='append')
    args = parser.parse_args()
    print(json.dumps(compare(json.loads(args.snapshot.read_text()), Settings(), args.output,
                             args.mode or ('single', 'no-critic', 'full')), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
