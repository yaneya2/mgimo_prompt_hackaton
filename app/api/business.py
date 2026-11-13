"""Local HTTP boundary for UI, durable commands and immutable exports."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from app.storage.database import new_id, encode, now
from app.storage.validation import validate, StorageError, Conflict, NotFound
from app.storage.controls import pause, revise, exclude
from app.llm.summary import aggregate_usage
from app.workflow.results import error_for
from app.reports import render

router=APIRouter(prefix='/api/v1')


def snapshot_view(store,run_id):
    value=store.get_snapshot(run_id)
    if value['brief']['version']!=value['project']['current_brief_version']:
        value['run']['quality']='draft'
        for artifact in value['artifacts']: artifact['freshness']='stale'
    return value


class Commands:
    def __init__(self,store):
        self.store=store;self.path=store.data_dir/'api.sqlite3';self.export_path=store.data_dir/'exports.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS commands(scope TEXT,key TEXT,fingerprint TEXT,status INTEGER,response TEXT,PRIMARY KEY(scope,key))')
        with sqlite3.connect(self.export_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY,metadata TEXT,content TEXT,format TEXT)')

    def execute(self,scope,key,body,callback,status):
        if not isinstance(key,str) or not 1<=len(key)<=128:raise StorageError('Требуется Idempotency-Key длиной 1–128 символов.')
        digest=hashlib.sha256(encode(body).encode()).hexdigest()
        with sqlite3.connect(self.path,timeout=10,isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute('SELECT fingerprint,status,response FROM commands WHERE scope=? AND key=?',(scope,key)).fetchone()
                if row:
                    if row[0]!=digest:raise Conflict('Ключ команды уже использован с другим содержимым.')
                    db.execute('COMMIT');return row[1],json.loads(row[2])
                # Serialize only short storage commands, never model/network work.
                identity='cmd_'+hashlib.sha256((scope+key).encode()).hexdigest()[:28]
                response=callback(identity)
                db.execute('INSERT INTO commands VALUES (?,?,?,?,?)',(scope,key,digest,status,encode(response)))
                db.execute('COMMIT');return status,response
            except BaseException:
                if db.in_transaction:db.execute('ROLLBACK')
                raise

    def export(self,project_id,body,identity):
        snap=snapshot_view(self.store,body['run_id']);run=snap['run']
        if run['project_id']!=project_id:raise NotFound('Запуск не принадлежит проекту.')
        if run['revision']!=body['expected_revision']:raise Conflict('Перед выгрузкой обновите результат.')
        historical=snap['brief']['version']!=snap['project']['current_brief_version']
        if not body['allow_draft'] and (historical or run['quality']!='ready' or run['status']!='completed'):
            raise Conflict('Для этого результата требуется явное согласие на черновую выгрузку.')
        ext=body['format']; eid='export_'+identity[4:]
        meta=dict(id=eid,run_id=run['id'],run_revision=run['revision'],brief_version=run['brief_version'],quality=run['quality'],
                  format=ext,download_url='/api/v1/exports/'+eid+'/download')
        content=(render.html if ext=='html' else render.markdown)(snap,self.store.list_inputs(run['id']))
        with sqlite3.connect(self.export_path) as db:
            db.execute('INSERT OR IGNORE INTO exports VALUES (?,?,?,?)',(eid,encode(meta),content,ext))
        return meta


def define(method,path,*,schema=None,status=200):
    def decorator(fn):
        async def endpoint(request:Request):
            run_id=request.path_params.get('run_id')
            try:
                store=request.app.state.storage
                if method=='POST':
                    origin=request.headers.get('origin')
                    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):
                        return JSONResponse(error_for(run_id,'VALIDATION_ERROR','Запрос должен исходить из локального интерфейса.'),status_code=403)
                    if 'application/json' not in request.headers.get('content-type',''):
                        raise StorageError('Требуется JSON-запрос.')
                    raw=await request.body()
                    if len(raw)>512000:return JSONResponse(error_for(run_id,'VALIDATION_ERROR','Слишком большой запрос.'),status_code=413)
                    try:body=json.loads(raw)
                    except (ValueError,UnicodeError):raise StorageError('Некорректный JSON.') from None
                    validate(schema,body)
                    command=request.app.state.commands
                    code,result=command.execute(request.url.path,request.headers.get('idempotency-key'),body,
                        lambda identity:fn(request,store,body,identity,**request.path_params),status)
                    return JSONResponse(result,status_code=code)
                result=fn(request,store,**request.path_params)
                return result if isinstance(result,Response) else JSONResponse(result)
            except NotFound:
                return JSONResponse(error_for(run_id,'NOT_FOUND','Объект не найден.'),status_code=404)
            except Conflict as exc:
                return JSONResponse(error_for(run_id,'VERSION_CONFLICT',str(exc)),status_code=409)
            except StorageError as exc:
                return JSONResponse(error_for(run_id,'VALIDATION_ERROR',str(exc)),status_code=422)
            except (OSError,sqlite3.Error):
                return JSONResponse(error_for(run_id,'INTERNAL_ERROR','Хранилище временно недоступно. Результаты не потеряны.'),status_code=503)
        router.add_api_route(path,endpoint,methods=[method],name=fn.__name__,status_code=status)
        return fn
    return decorator


@define('GET','/projects')
def projects(request,store):return store.list_projects()


@define('POST','/projects',schema='CreateProject',status=201)
def create_project(request,store,body,identity):
    if not body['name'].strip():raise StorageError('Укажите название проекта.')
    pid='project_'+identity[4:]
    try:return store.create_project(body['name'],body['brief'],project_id=pid)
    except Conflict:
        existing=next(p for p in store.list_projects() if p['id']==pid)
        if existing['name']!=body['name'] or store.get_brief(pid)['input']!=body['brief']:raise Conflict('Ключ использован для другого проекта.')
        return existing


@define('GET','/projects/{project_id}/brief')
def brief(request,store,project_id):return store.get_brief(project_id)


@define('POST','/projects/{project_id}/brief-versions',schema='ReviseBrief',status=201)
def revise_brief(request,store,body,identity,project_id):
    return revise(store,project_id,body['brief'],body['expected_brief_version'])


@define('GET','/projects/{project_id}/runs')
def runs(request,store,project_id):return store.list_runs(project_id)


@define('POST','/projects/{project_id}/runs',schema='CreateRun',status=202)
def start(request,store,body,identity,project_id):
    rid='run_'+identity[4:]
    try:return store.create_run(project_id,body['limits'],brief_version=body['brief_version'],run_id=rid)
    except Conflict:
        # Crash-retry may recover the already-created deterministic run; a different command still conflicts.
        try:
            existing=store.get_run(rid)
            if existing['brief_version']!=body['brief_version'] or store.get_limits(rid)!=body['limits']:raise Conflict('Ключ использован для другого запуска.')
            return existing
        except NotFound:raise Conflict('У проекта уже есть незавершённый запуск либо версия брифа устарела.') from None


@define('GET','/runs/{run_id}')
def run(request,store,run_id):return store.get_run(run_id)


@define('GET','/runs/{run_id}/snapshot')
def snapshot(request,store,run_id):return snapshot_view(store,run_id)


@define('GET','/runs/{run_id}/artifacts')
def artifacts(request,store,run_id):return snapshot_view(store,run_id)['artifacts']


@define('GET','/runs/{run_id}/usage')
def usage(request,store,run_id):return store.list_usage(run_id)


@define('GET','/runs/{run_id}/usage-summary')
def usage_summary(request,store,run_id):return aggregate_usage(store.list_usage(run_id),run_id)


@define('GET','/runs/{run_id}/inputs')
def inputs(request,store,run_id):return store.list_inputs(run_id)


@define('GET','/runs/{run_id}/events')
def events(request,store,run_id):
    return [{k:v for k,v in job.items() if k in ('id','stage','state','created_at','finished_at','checkpoint_id')}
            for job in store.list_jobs(run_id)]


@define('POST','/runs/{run_id}/pause',schema='RunCommand',status=202)
def pause_run(request,store,body,identity,run_id):return pause(store,run_id,body['expected_revision'])


@define('POST','/runs/{run_id}/resume',schema='RunCommand',status=202)
def resume_run(request,store,body,identity,run_id):return store.requeue_paused(run_id,expected_revision=body['expected_revision'])


@define('POST','/runs/{run_id}/answers',schema='AnswerQuestions',status=202)
def answers(request,store,body,identity,run_id):return store.submit_answers(run_id,body['answers'],expected_revision=body['expected_revision'])


@define('POST','/runs/{run_id}/selection',schema='ChooseOption',status=202)
def select(request,store,body,identity,run_id):
    return store.submit_selection(run_id,body['option_id'],body['option_version'],body['rationale'],expected_revision=body['expected_revision'])


@define('POST','/runs/{run_id}/exclude-source',schema='ExcludeSource',status=202)
def exclude_source(request,store,body,identity,run_id):return exclude(store,run_id,body)


@define('POST','/projects/{project_id}/exports',schema='ExportRequest',status=201)
def export(request,store,body,identity,project_id):return request.app.state.commands.export(project_id,body,identity)


@define('GET','/exports/{export_id}/download')
def download(request,store,export_id):
    with sqlite3.connect(request.app.state.commands.export_path) as db:
        row=db.execute('SELECT content,format FROM exports WHERE id=?',(export_id,)).fetchone()
    if not row:raise NotFound('Выгрузка не найдена.')
    content,fmt=row;ext='html' if fmt=='html' else 'md'
    return Response(content,media_type='text/html' if ext=='html' else 'text/markdown',headers={
        'Content-Disposition':f'attachment; filename="strategy.{ext}"',
        'Content-Security-Policy':"sandbox; default-src 'none'; style-src 'unsafe-inline'",
        'X-Content-Type-Options':'nosniff'})
