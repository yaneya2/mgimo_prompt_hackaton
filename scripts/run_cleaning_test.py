"""Real model calls with an explicitly fictional, isolated internal-data test case."""
import json
from pathlib import Path
from app.config import Settings
from app.storage import Storage
from app.agents.runtime import build_stages
from app.worker import WorkerService

ROOT=Path(__file__).resolve().parents[1]
MATERIALS=ROOT/'materials/realistic-cleaning'
DATA=ROOT/'data/realistic-cleaning'

def main():
    settings=Settings(data_dir=DATA,research_materials_dir=MATERIALS,
        research_files=['01-business.md','02-orders.md','03-funnel.md','04-interviews.md'],
        tavily_api_key=None,model_timeout_seconds=None)
    store=Storage(DATA)
    marker=MATERIALS/'run.json'
    if marker.exists(): run_id=json.loads(marker.read_text())['run_id']
    else:
        project=store.create_project('ТЕСТ: Чистый четверг — дневная загрузка',json.loads((MATERIALS/'brief.json').read_text()))
        run=store.create_run(project['id'],dict(max_model_calls=30,max_search_calls=10,max_repair_cycles=2,timeout_seconds=0,budget=None))
        run_id=run['id'];marker.write_text(json.dumps(dict(project_id=project['id'],run_id=run_id),ensure_ascii=False,indent=2))
    print('RUN',run_id,flush=True)
    try: WorkerService(store,build_stages(settings,store)).run(once=True)
    finally:
        snapshot=store.get_snapshot(run_id)
        (MATERIALS/'snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
        (MATERIALS/'events.json').write_text(json.dumps(store.list_events(run_id),ensure_ascii=False,indent=2))
        print(json.dumps(snapshot['run'],ensure_ascii=False),flush=True)
        print('QUESTIONS',json.dumps(snapshot['questions'],ensure_ascii=False),flush=True)

if __name__=='__main__':main()
