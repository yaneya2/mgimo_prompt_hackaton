"""Durable conservative reservations; human waiting time is excluded."""
from pathlib import Path
import sqlite3
import time


class LimitExceeded(ValueError):
    pass


class CallGate:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'agent-limits.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS reservations (run_id TEXT PRIMARY KEY, model INTEGER, search INTEGER, elapsed REAL, active_since REAL)')

    def begin(self, request):
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO reservations VALUES (?, 0, 0, 0, NULL)', (request['run_id'],))
            # A killed invocation remains active: its wall time is conservatively charged on restart.
            db.execute('UPDATE reservations SET elapsed=elapsed+CASE WHEN active_since IS NULL THEN 0 ELSE MAX(0, ?-active_since) END, active_since=? WHERE run_id=?',
                       (time.time(), time.time(), request['run_id']))

    def finish(self, request):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE reservations SET elapsed=elapsed+CASE WHEN active_since IS NULL THEN 0 ELSE MAX(0, ?-active_since) END, active_since=NULL WHERE run_id=?',
                       (time.time(), request['run_id']))

    def reserve(self, request, kind, count):
        if kind not in ('model', 'search') or count < 1:
            raise ValueError('Invalid reservation')
        limits = request['limits']
        if limits['budget'] is not None:
            raise LimitExceeded('Денежный лимит нельзя гарантировать при неизвестной стоимости; внешние вызовы остановлены.')
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO reservations VALUES (?, 0, 0, 0, NULL)', (request['run_id'],))
            row = db.execute('SELECT model, search, elapsed, active_since FROM reservations WHERE run_id=?', (request['run_id'],)).fetchone()
            elapsed = row[2] + (max(0, time.time() - row[3]) if row[3] is not None else 0)
            if elapsed >= limits['timeout_seconds']:
                raise LimitExceeded('Лимит активного времени запуска исчерпан; создайте новый запуск для продолжения.')
            used = row[0 if kind == 'model' else 1]
            if used + count > limits['max_' + kind + '_calls']:
                raise LimitExceeded('Лимит вызовов ' + kind + ' исчерпан.')
            db.execute('UPDATE reservations SET ' + kind + '=' + kind + '+? WHERE run_id=?', (count, request['run_id']))
