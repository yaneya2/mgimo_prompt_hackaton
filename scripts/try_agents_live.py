"""Repeatable real-stage run with explicit model, preserved evidence, Usage and human boundary."""
import argparse
import json
from pathlib import Path
import time

from app.agents.runtime import build_stages
from app.config import Settings
from app.storage import Storage
from app.worker import WorkerService


def preflight(settings, mode):
    missing = []
    if not settings.model_name:
        missing.append('Требуется точный APP_MODEL_NAME / --model.')
    if mode == 'web' and not settings.tavily_api_key:
        missing.append('APP_TAVILY_API_KEY не задан: настоящий Search/Extract недоступен.')
    if mode == 'provided' and not settings.research_files:
        missing.append('Для provided нужны явно выбранные файлы --file и каталог --materials.')
    return dict(status='blocked' if missing else 'configured', mode=mode, model_requested=settings.model_name,
                provider=settings.model_provider, missing=missing, services_called=False)


def save_evidence(store, run_id, directory, settings, mode, elapsed):
    snapshot=store.get_snapshot(run_id)
    records=dict(snapshot=snapshot, inputs=store.list_inputs(run_id), limits=store.get_limits(run_id),
                 registry=build_stages(settings, store).manifest(), mode=mode, duration_seconds=elapsed,
                 independent_acceptance=False, live_web_verified=mode=='web' and any(s['kind']=='web' and s['verification']=='read'
                                                                          for s in snapshot['data']['sources']))
    (directory/'evidence.json').write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
    (directory/'snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n')
    (directory/'inputs.json').write_text(json.dumps(records['inputs'],ensure_ascii=False,indent=2)+'\n')
    (directory/'usage.json').write_text(json.dumps(snapshot['usage'],ensure_ascii=False,indent=2)+'\n')
    return dict(run_id=run_id,status=snapshot['run']['status'],stage=snapshot['run']['stage'],quality=snapshot['run']['quality'],
                model_requested=settings.model_name,usage_events=len(snapshot['usage']),evidence=str(directory/'evidence.json'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    parser.add_argument('--brief',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path,default=Path('data/live-agents'))
    parser.add_argument('--mode',choices=['web','provided'],default='web')
    parser.add_argument('--materials',type=Path,default=Path('materials'))
    parser.add_argument('--file',action='append',default=[])
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--run-id',help='Drain an existing queued run after actual answers/selection; never submits for the human')
    args=parser.parse_args()
    args.data_dir.mkdir(parents=True,exist_ok=True)
    settings=Settings(model_name=args.model,data_dir=args.data_dir,model_timeout_seconds=180,
                      research_materials_dir=args.materials,research_files=args.file,model_max_tokens=8192)
    check=preflight(settings,args.mode)
    (args.data_dir/'preflight.json').write_text(json.dumps(check,ensure_ascii=False,indent=2)+'\n')
    if args.check_only or check['missing']:
        print(json.dumps(check,ensure_ascii=False,indent=2));return 0 if not check['missing'] else 2
    store=Storage(settings.data_dir)
    if args.run_id:
        run_id=args.run_id
    else:
        project=store.create_project('Живая проверка',json.loads(args.brief.read_text()))
        run_id=store.create_run(project['id'],dict(max_model_calls=20,max_search_calls=10,max_repair_cycles=2,
                                                  timeout_seconds=1800,budget=None))['id']
    started=time.monotonic()
    try:
        WorkerService(store,build_stages(settings,store)).run(once=True)
    finally:
        result=save_evidence(store,run_id,args.data_dir,settings,args.mode,time.monotonic()-started)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status'] in ('waiting_for_input','completed') else 1


if __name__=='__main__':
    raise SystemExit(main())
