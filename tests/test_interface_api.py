"""HTTP acceptance: user gates, persistence, version edits, safe exports and control races."""
from copy import deepcopy
from dataclasses import replace
import json
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.worker import WorkerService
from app.workflow.demo import build_stages
from app.storage.validation import validate,validate_snapshot
from app.storage.controls import pause
from app.reports import render
from scripts.try_workflow import demo_brief,LIMITS


def post(client,path,body,key=None):
    return client.post('/api/v1'+path,json=body,headers={'Idempotency-Key':key or uuid4().hex})


def create(client):
    brief=demo_brief('Новый сервис управления возвратами магазина');brief['budget']={'amount':'50000','currency':'RUB'}
    p=post(client,'/projects',dict(name='API acceptance',brief=brief)).json()
    r=post(client,f"/projects/{p['id']}/runs",dict(brief_version=1,limits=LIMITS)).json()
    return p,r


def test_http_round_trip_selection_and_immutable_exports(tmp_path):
    config=Settings(_env_file=None,data_dir=tmp_path,stage_factory='demo')
    with TestClient(create_app(config)) as client:
        p,r=create(client);store=client.app.state.storage
        WorkerService(store,build_stages()).run(once=True)
        snap=client.get(f"/api/v1/runs/{r['id']}/snapshot").json()
        validate('Snapshot',snap)
        assert snap['run']['stage']=='selection' and not snap['data']['decisions']
        assert client.get('/').status_code==200
        assert 'default-src' in client.get('/').headers['content-security-policy']
        assert client.get(f"/api/v1/runs/{r['id']}/events").json()
        assert client.get(f"/api/v1/runs/{r['id']}/usage-summary").json()['totals']['events']==0
        option=snap['data']['options'][1]
        cmd=dict(expected_revision=snap['run']['revision'],option_id=option['id'],option_version=option['version'],rationale='Выбор второго сегмента')
        first=post(client,f"/runs/{r['id']}/selection",cmd,key='selection-once')
        assert first.status_code==202
        assert post(client,f"/runs/{r['id']}/selection",cmd,key='selection-once').json()==first.json()
        WorkerService(store,build_stages()).run(once=True)
        final=client.get(f"/api/v1/runs/{r['id']}/snapshot").json()
        assert final['run']['status']=='completed' and final['run']['quality']=='draft'
        command=dict(run_id=r['id'],expected_revision=final['run']['revision'],format='html',allow_draft=False)
        assert post(client,f"/projects/{p['id']}/exports",command).status_code==409
        command['allow_draft']=True
        exported=post(client,f"/projects/{p['id']}/exports",command,key='export-once')
        assert exported.status_code==201
        validate('Export',exported.json())
        content=client.get(exported.json()['download_url'])
        assert 'Выбор второго сегмента' in content.text and 'ЧЕРНОВАЯ' in content.text
        assert 'attachment' in content.headers['content-disposition']
        url=exported.json()['download_url']
    with TestClient(create_app(config)) as client:
        assert client.get(url).content==content.content
        assert client.get(f"/api/v1/runs/{r['id']}/snapshot").json()['run']['status']=='completed'


def test_idempotency_validation_and_double_start(tmp_path):
    with TestClient(create_app(Settings(_env_file=None,data_dir=tmp_path))) as client:
        body=dict(name='One',brief=demo_brief())
        assert client.post('/api/v1/projects',json=body).status_code==422
        one=post(client,'/projects',body,'same')
        assert one.status_code==201 and post(client,'/projects',body,'same').json()==one.json()
        assert post(client,'/projects',dict(body,name='Other'),'same').status_code==409
        assert len(client.get('/api/v1/projects').json())==1
        command=dict(brief_version=1,limits=LIMITS);path=f"/projects/{one.json()['id']}/runs"
        first=post(client,path,command,'start');assert first.status_code==202
        assert post(client,path,command,'start').json()==first.json()
        assert post(client,path,command,'other-key').status_code==409
        assert client.post('/api/v1/projects',json=body,headers={'Idempotency-Key':'cross','Origin':'https://evil.example'}).status_code==403
        assert client.get('/api/v1/fixtures/not-a-file').status_code==404
        assert client.get('/api/v1/projects',headers={'host':'evil.example'}).status_code==400
        assert post(client,'/projects',dict(body,unknown='private')).status_code==422


def test_revision_stops_waiting_run_and_marks_history_without_deleting(tmp_path):
    with TestClient(create_app(Settings(_env_file=None,data_dir=tmp_path,stage_factory='demo'))) as client:
        p,r=create(client);store=client.app.state.storage
        WorkerService(store,build_stages()).run(once=True)
        old=store.get_snapshot(r['id']);changed=deepcopy(old['brief']['input'])
        changed['budget']['amount']='25000';changed['constraints']+=['Платная реклама запрещена']
        response=post(client,f"/projects/{p['id']}/brief-versions",dict(expected_brief_version=1,brief=changed))
        assert response.status_code==201 and response.json()['version']==2
        historical=client.get(f"/api/v1/runs/{r['id']}/snapshot").json()
        assert historical['run']['status']=='incomplete'
        assert all(a['freshness']=='stale' for a in historical['artifacts'])
        assert store.get_checkpoint(old['run']['checkpoint_id'])['artifacts']==old['artifacts']
        new=post(client,f"/projects/{p['id']}/runs",dict(brief_version=2,limits=LIMITS)).json()
        WorkerService(store,build_stages()).run(once=True)
        snap=store.get_snapshot(new['id'])
        assert snap['brief']['input']['budget']['amount']=='25000'
        assert snap['data']['options'] and not snap['data']['decisions']
        assert len(client.get(f"/api/v1/projects/{p['id']}/runs").json())==2


def test_user_pause_during_model_stage_commits_then_stops_before_next_call(tmp_path):
    with TestClient(create_app(Settings(_env_file=None,data_dir=tmp_path))) as client:
        p,r=create(client);store=client.app.state.storage
        registry=build_stages();spec=registry.stages['brief'];called=[]
        def handler(req,ctx):
            called.append('brief');live=store.get_run(r['id'])
            result=post(client,f"/runs/{r['id']}/pause",dict(expected_revision=live['revision']))
            assert result.status_code==202 and result.json()['status']=='pause_requested'
            return spec.handler(req,ctx)
        registry=replace(registry,stages=dict(registry.stages,brief=replace(spec,handler=handler)))
        WorkerService(store,registry).run(once=True)
        final=store.get_snapshot(r['id'])
        assert final['run']['status']=='paused' and final['run']['stage']=='research'
        assert [j['stage'] for j in store.list_jobs(r['id'])]==['brief']
        validate_snapshot(final)
        assert post(client,f"/runs/{r['id']}/resume",dict(expected_revision=final['run']['revision'])).status_code==202
        WorkerService(store,registry).run(once=True)
        assert store.get_run(r['id'])['stage']=='selection' and called==['brief']


def test_queued_pause_is_immediate_and_revise_while_running_refuses(tmp_path):
    with TestClient(create_app(Settings(_env_file=None,data_dir=tmp_path))) as client:
        p,r=create(client);store=client.app.state.storage
        paused=post(client,f"/runs/{r['id']}/pause",dict(expected_revision=r['revision'])).json()
        assert paused['status']=='paused'
        post(client,f"/runs/{r['id']}/resume",dict(expected_revision=paused['revision']))
        with store.worker() as worker:
            worker.claim_run(r['id'])
            assert post(client,f"/projects/{p['id']}/brief-versions",dict(expected_brief_version=1,brief=demo_brief())).status_code==409


def test_renderer_escapes_untrusted_html_and_includes_quotes_versions():
    from pathlib import Path
    snap=json.loads((Path(__file__).parents[1]/'contracts/examples/success.json').read_text())
    snap['project']['name']='<img src=x onerror=alert(1)>'
    snap['data']['sources'][0]['content_excerpt']='<script>alert("secret")</script>'
    content=render.html(snap)
    assert '<script>' not in content and '<img src=x' not in content
    assert '&lt;script&gt;' in content and 'Версия брифа' in content
    assert render.safe_url('javascript:alert(1)') is None
    assert render.safe_url('https://user:password@example.org') is None


def test_excluded_source_metadata_follows_new_run_and_hides_old_source_text(tmp_path):
    from app.storage.controls import excluded_metadata
    with TestClient(create_app(Settings(_env_file=None,data_dir=tmp_path))) as client:
        p,r=create(client);store=client.app.state.storage
        WorkerService(store,build_stages()).run(once=True)
        snap=store.get_snapshot(r['id']);src=snap['data']['sources'][0]
        response=post(client,f"/runs/{r['id']}/exclude-source",dict(expected_revision=snap['run']['revision'],source_id=src['id'],source_version=src['version'],reason='Проверка происхождения'))
        assert response.status_code==202
        new=response.json();assert new['id']!=r['id'] and new['brief_version']==2
        meta=excluded_metadata(store,p['id'],[src['id']])
        assert meta[0]['id']==src['id'] and 'content_excerpt' not in meta[0]
        assert src['id'] in store.get_brief(p['id'])['input']['excluded_source_ids']
