"""Opt-in live B1 smoke check through the production adapter; uses account limits."""
import json
from pathlib import Path

from app.agents.runtime import AgentRuntime
from app.config import Settings
from app.llm.usage import JsonlUsageSink
from app.storage.validation import empty_data, validate
from app.workflow.stages import StageContext
from app.storage.database import new_id, now


def main():
    settings = Settings()
    directory = settings.data_dir / 'agent-checks'
    directory.mkdir(parents=True, exist_ok=True)
    run_id = new_id('run')
    brief = json.loads(Path('examples/agents/tutor.json').read_text())
    request = dict(schema_version='1.0.0', run_id=run_id, project_id=new_id('project'), stage='brief',
                   brief=dict(project_id='', version=1, created_at=now(), author='user', input=brief),
                   input_data=empty_data(), input_artifacts=[],
                   limits=dict(max_model_calls=3, max_search_calls=1, max_repair_cycles=0, timeout_seconds=300, budget=None),
                   prompt_version='brief-v1', model_id=settings.model_name or 'provider-default')
    request['brief']['project_id'] = request['project_id']
    validate('StageRequest', request)
    runtime = AgentRuntime(settings, JsonlUsageSink(directory / 'usage.jsonl'))
    result = runtime(request, StageContext([], [], 0, False))
    validate('StageResult', result)
    destination = directory / (run_id + '.json')
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(dict(outcome=result['outcome'], capabilities=len(result['data']['capabilities']),
                          questions=len(result['questions']), result=str(destination)), ensure_ascii=False))


if __name__ == '__main__':
    main()
