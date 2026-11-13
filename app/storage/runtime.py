"""Durable per-attempt reservations, bounded execution, local cache and safe events."""
from datetime import datetime, timezone
import json
import re

from app.llm.accounting import fingerprint
from app.execution import PauseRequested
from app.llm.summary import aggregate_usage
from app.storage.database import encode, new_id, now
from app.storage.validation import require, validate

RUNTIME_DDL = [
    """CREATE TABLE external_calls (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), job_id TEXT NOT NULL REFERENCES jobs(id),
        stage TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('model','search')),
        maximum_cost TEXT, created_at TEXT NOT NULL, usage_id TEXT UNIQUE REFERENCES usage(id))""",
    'CREATE INDEX calls_by_run ON external_calls(run_id)',
    """CREATE TABLE response_cache (
        run_id TEXT NOT NULL REFERENCES runs(id), key TEXT NOT NULL, payload TEXT NOT NULL,
        created_at TEXT NOT NULL, ttl_seconds INTEGER NOT NULL, PRIMARY KEY(run_id, key))""",
    """CREATE TABLE run_events (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), stage TEXT NOT NULL,
        kind TEXT NOT NULL, code TEXT NOT NULL, created_at TEXT NOT NULL)""",
    'CREATE INDEX events_by_run ON run_events(run_id)',
]

MESSAGES = {
    'MODEL_CALL_LIMIT': 'Исчерпан лимит попыток вызова модели.',
    'SEARCH_CALL_LIMIT': 'Исчерпан лимит попыток вызова инструментов.',
    'STEP_LIMIT': 'Исчерпан лимит выполнения этапов.',
    'ACTIVE_TIME_LIMIT': 'Исчерпан лимит активного времени запуска.',
    'BUDGET_LIMIT': 'Недостаточно бюджета для следующей попытки.',
    'COST_RESERVE_REQUIRED': 'Для денежного лимита требуется настроенный резерв стоимости вызова.',
    'UNKNOWN_PREVIOUS_COST': 'Стоимость предыдущих вызовов неизвестна: безопасно оценить остаток бюджета нельзя.',
    'CURRENCY_MISMATCH': 'Валюты бюджета и расходов не совпадают; автоматический пересчёт валют не выполняется.',
}


class LimitReached(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def units(amount):
    whole, _, fraction = amount.partition('.')
    return int(whole) * 1_000_000 + int((fraction + '000000')[:6])


def money(value, currency):
    whole, fraction = divmod(max(0, value), 1_000_000)
    return dict(amount=(f'{whole}.{fraction:06d}'.rstrip('0').rstrip('.') if fraction else str(whole)), currency=currency)


def event(db, run_id, stage, kind, code):
    # No exception text, prompts, inputs, paths, URLs or arbitrary agent strings.
    require(re.fullmatch(r'[a-z_]{1,40}', kind) and re.fullmatch(r'[A-Za-z_]{1,80}', code), 'Invalid event code')
    db.execute('INSERT INTO run_events VALUES (?, ?, ?, ?, ?, ?)',
               (new_id('event'), run_id, stage, kind, code, now()))


def active_seconds(db, run_id):
    stamp = datetime.now(timezone.utc)
    return sum(max(0, ((datetime.fromisoformat(row['finished_at']) if row['finished_at'] else stamp)
                      - datetime.fromisoformat(row['created_at'])).total_seconds())
               for row in db.execute("SELECT created_at, finished_at FROM jobs WHERE run_id = ? AND owner != 'user-control'", (run_id,)))


def balances(db, storage, run_id, limits):
    usage = storage._usage(db, run_id)
    calls = [dict(row) for row in db.execute('SELECT * FROM external_calls WHERE run_id = ?', (run_id,))]
    linked = {call['usage_id'] for call in calls if call['usage_id']}
    legacy = [u for u in usage if not u['cached'] and u['id'] not in linked]
    counts = {kind: sum(c['kind'] == kind for c in calls) + sum((u['tool'] is None) == (kind == 'model')
                  for u in legacy if not (u['provider'] == 'local' and u['tool'] == 'read_file'))
              for kind in ('model', 'search')}
    limit = limits['budget']
    budget = dict(limit=limit, allocated=None, remaining_estimate=None, estimated=False,
                  unrecorded_attempts=sum(c['usage_id'] is None for c in calls), blocked_reason=None)
    if limit is not None:
        by_id = {u['id']: u for u in usage}
        total, unknown, mismatch = 0, False, False
        charges = [(by_id.get(c['usage_id']), json.loads(c['maximum_cost']) if c['maximum_cost'] else None) for c in calls]
        charges += [(u, None) for u in legacy]
        for recorded, reserve in charges:
            cost = recorded['cost'] if recorded else None
            basis = recorded['cost_basis'] if recorded else 'unknown'
            if any(value and value['currency'] != limit['currency'] for value in (cost, reserve)):
                mismatch = True
                continue
            if basis == 'reported' and cost is not None:
                total += units(cost['amount'])
            elif reserve is not None:
                total += max(units(reserve['amount']), units(cost['amount']) if cost else 0)
                budget['estimated'] = True
            else:
                unknown = True
        budget['blocked_reason'] = 'CURRENCY_MISMATCH' if mismatch else 'UNKNOWN_PREVIOUS_COST' if unknown else None
        if not budget['blocked_reason']:
            budget['allocated'] = money(total, limit['currency'])
            budget['remaining_estimate'] = money(units(limit['amount']) - total, limit['currency'])
            if total > units(limit['amount']):
                budget['blocked_reason'] = 'BUDGET_LIMIT'
    return counts, budget, usage


def summary(storage, run_id):
    with storage.transaction() as db:
        return summary_from_connection(storage, db, run_id)


def summary_from_connection(storage, db, run_id):
    run = storage._run(db, run_id)
    limits = json.loads(db.execute('SELECT limits_json FROM runs WHERE id = ?', (run_id,)).fetchone()[0])
    counts, budget, usage = balances(db, storage, run_id, limits)
    steps = db.execute("SELECT COUNT(*) FROM jobs WHERE run_id = ? AND owner != 'user-control'", (run_id,)).fetchone()[0]
    seconds = active_seconds(db, run_id)
    checkpoint = storage._payload(db, 'SELECT payload FROM checkpoints WHERE id = ?', (run['checkpoint_id'],)) if run['checkpoint_id'] else {}
    aggregates = aggregate_usage(usage, run_id)
    return dict(run_id=run_id, run_revision=run['revision'], status=run['status'], limits=limits,
                accounting_complete=aggregates['totals']['cost_complete'] and budget['unrecorded_attempts'] == 0,
                model_calls=counts['model'], search_calls=counts['search'], steps=steps,
                active_seconds=round(seconds, 3), remaining_seconds=round(max(0, limits['timeout_seconds'] - seconds), 3) if limits['timeout_seconds'] else None,
                budget=budget, usage=aggregates, stop_reason=checkpoint.get('graph_state', {}).get('stop_reason'))


class RunPolicy:
    def __init__(self, storage, request, job, state, *, inputs=()):
        self.storage, self.request, self.job = storage, request, job
        self.run_id, self.stage, self.limits = request['run_id'], request['stage'], request['limits']
        self.input_hash = fingerprint(dict(request=request, inputs=list(inputs),
                                          feedback=state['feedback'], repairs_used=state['repairs_used']))

    def check(self):
        with self.storage.transaction() as db:
            steps = db.execute("SELECT COUNT(*) FROM jobs WHERE run_id = ? AND owner != 'user-control'", (self.run_id,)).fetchone()[0]
            if steps > self.limits.get('max_steps', 100):
                raise LimitReached('STEP_LIMIT')
            if self.limits['timeout_seconds'] and active_seconds(db, self.run_id) >= self.limits['timeout_seconds']:
                raise LimitReached('ACTIVE_TIME_LIMIT')
            if self.limits['budget'] is not None:
                _, budget, _ = balances(db, self.storage, self.run_id, self.limits)
                if budget['blocked_reason']:
                    raise LimitReached(budget['blocked_reason'])

    def reserve(self, kind, timeout, maximum_cost=None):
        require(kind in ('model', 'search') and (timeout is None or timeout > 0), 'Invalid external action reservation')
        if maximum_cost is not None:
            validate('Money', maximum_cost)
        with self.storage.transaction(write=True) as db:
            run = self.storage._run(db, self.run_id)
            if run['status'] == 'pause_requested':
                raise PauseRequested()
            require(run['status'] == 'running', 'External action requires an active run')
            remaining = self.limits['timeout_seconds'] - active_seconds(db, self.run_id) if self.limits['timeout_seconds'] else None
            if remaining is not None and remaining <= 0:
                raise LimitReached('ACTIVE_TIME_LIMIT')
            counts, budget, _ = balances(db, self.storage, self.run_id, self.limits)
            if counts[kind] >= self.limits['max_model_calls' if kind == 'model' else 'max_search_calls']:
                raise LimitReached('MODEL_CALL_LIMIT' if kind == 'model' else 'SEARCH_CALL_LIMIT')
            if self.limits['budget'] is not None:
                if budget['blocked_reason']:
                    raise LimitReached(budget['blocked_reason'])
                if maximum_cost is None:
                    raise LimitReached('COST_RESERVE_REQUIRED')
                if maximum_cost['currency'] != self.limits['budget']['currency']:
                    raise LimitReached('CURRENCY_MISMATCH')
                if units(maximum_cost['amount']) > units(budget['remaining_estimate']['amount']):
                    raise LimitReached('BUDGET_LIMIT')
            call_id = new_id('call')
            db.execute('INSERT INTO external_calls VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
                       (call_id, self.run_id, self.job['id'], self.stage, kind,
                        encode(maximum_cost) if maximum_cost else None, now()))
            event(db, self.run_id, self.stage, 'external_reserved', kind)
        return dict(call_id=call_id, timeout_seconds=min(v for v in (timeout, remaining) if v is not None) if any(v is not None for v in (timeout, remaining)) else None)

    def cache_key(self, identity):
        return fingerprint(dict(version=2, input_hash=self.input_hash, stage=self.stage, identity=identity))

    def cached(self, key, max_age=None):
        with self.storage.transaction() as db:
            row = db.execute('SELECT * FROM response_cache WHERE run_id = ? AND key = ?', (self.run_id, key)).fetchone()
            if row is None:
                return None
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['created_at'])).total_seconds()
            ttl = min(row['ttl_seconds'], self.limits.get('cache_ttl_seconds', 3600), max_age if max_age is not None else 86400)
            if ttl <= 0 or not 0 <= age <= ttl:
                return None
            try:
                envelope = json.loads(row['payload'])
                if envelope['hash'] == fingerprint(envelope['value']):
                    return envelope['value']
            except (ValueError, KeyError, TypeError):
                pass
        self.invalidate_cache(key)
        return None

    def save_cache(self, key, payload, ttl=3600):
        ttl = min(ttl, self.limits.get('cache_ttl_seconds', 3600))
        encoded = encode(dict(value=payload, hash=fingerprint(payload)))
        if ttl <= 0 or len(encoded.encode()) > 1_048_576:
            return
        with self.storage.transaction(write=True) as db:
            db.execute('INSERT OR REPLACE INTO response_cache VALUES (?, ?, ?, ?, ?)',
                       (self.run_id, key, encoded, now(), ttl))
            event(db, self.run_id, self.stage, 'cache_stored', 'SUCCESS')

    def invalidate_cache(self, key):
        with self.storage.transaction(write=True) as db:
            db.execute('DELETE FROM response_cache WHERE run_id = ? AND key = ?', (self.run_id, key))
            event(db, self.run_id, self.stage, 'cache_invalidated', 'INVALID_RESPONSE')
