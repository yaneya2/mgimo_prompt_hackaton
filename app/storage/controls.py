"""Boundary-safe user controls used by the HTTP integration."""
import json
from app.storage.database import now, encode
from app.storage.validation import Conflict, require, validate


def pause(storage, run_id, revision):
    with storage.transaction(write=True) as db:
        run=storage._run(db,run_id)
        if run['revision']!=revision: raise Conflict('Состояние изменилось; обновите страницу.')
        if run['status'] in ('paused','pause_requested'): return run
        if run['status'] not in ('queued','running'): raise Conflict('Этот запуск уже остановлен или завершён.')
        executing=db.execute("SELECT 1 FROM jobs WHERE run_id=? AND state='running'",(run_id,)).fetchone()
        if executing:
            # An in-flight stage owns its revision until commit. Only the control flag changes.
            run.update(status='pause_requested',updated_at=now())
        else:
            run.update(status='paused',revision=revision+1,updated_at=now())
        storage._write_run(db,run)
        return run


def revise(storage, project_id, brief_input, expected_version, *, expected_run=None):
    """Supersede a stopped run atomically; retain its immutable checkpoints as history."""
    with storage.transaction(write=True) as db:
        if expected_run:
            live=storage._run(db,expected_run[0])
            if live['revision']!=expected_run[1]: raise Conflict('Состояние изменилось.')
        project=storage._payload(db,'SELECT payload FROM projects WHERE id=?',(project_id,))
        if project['current_brief_version']!=expected_version: raise Conflict('Версия брифа изменилась.')
        active=db.execute("SELECT payload FROM runs WHERE project_id=? AND status IN ('queued','running','pause_requested','paused','waiting_for_input')",(project_id,)).fetchone()
        if active:
            run=json.loads(active[0])
            if run['status'] in ('running','pause_requested'): raise Conflict('Дождитесь паузы перед изменением условий.')
            run.update(status='incomplete',quality='draft',revision=run['revision']+1,updated_at=now(),error=None)
            storage._write_run(db,run)
        version=expected_version+1
        brief=dict(project_id=project_id,version=version,created_at=now(),author='user',input=brief_input)
        validate('BriefVersion',brief)
        db.execute('INSERT INTO briefs VALUES (?,?,?)',(project_id,version,encode(brief)))
        project['current_brief_version']=version
        db.execute('UPDATE projects SET payload=? WHERE id=?',(encode(project),project_id))
        return brief


def exclude(storage, run_id, command):
    snapshot=storage.get_snapshot(run_id);run=snapshot['run']
    if run['revision']!=command['expected_revision']: raise Conflict('Состояние изменилось.')
    if run['status'] not in ('paused','waiting_for_input','completed','incomplete','failed'):
        raise Conflict('Сначала приостановите исследование.')
    src=next((s for s in snapshot['data']['sources'] if s['id']==command['source_id'] and s['version']==command['source_version']),None)
    require(src is not None,'Источник отсутствует или обновлён.')
    brief=snapshot['brief']['input']
    brief['excluded_source_ids']=list(dict.fromkeys(brief['excluded_source_ids']+[src['id']]))
    brief['constraints']+=['Источник исключён пользователем: '+src['title']+'. Причина: '+command['reason']]
    revised=revise(storage,run['project_id'],brief,snapshot['brief']['version'],expected_run=(run_id,command['expected_revision']))
    return storage.create_run(run['project_id'],storage.get_limits(run_id),brief_version=revised['version'])


def excluded_metadata(storage, project_id, identities):
    found=[]
    with storage.transaction() as db:
        for identity in identities:
            row=db.execute("SELECT payload FROM entities WHERE project_id=? AND kind='source' AND id=? ORDER BY version DESC LIMIT 1",(project_id,identity)).fetchone()
            if row:
                value=json.loads(row[0]);found.append({k:value[k] for k in ('id','kind','url','title')})
    return tuple(found)
