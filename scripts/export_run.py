"""Export observed run metrics for the submission without model calls or state changes."""
import argparse
import csv
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import tempfile

from app.storage import Storage, StorageError
from app.storage.evidence import run_evidence

ROOT = Path(__file__).resolve().parents[1]


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def csv_cell(value):
    text = '' if value is None else str(value)
    # Spreadsheet import must not execute labels received from a provider.
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


def usage_csv(usage):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    keys = ['id', 'call_id', 'attempt', 'stage', 'provider', 'model', 'tool', 'started_at', 'duration_ms',
            'input_tokens', 'output_tokens', 'cost_basis', 'outcome', 'cached', 'cache_origin_id']
    writer.writerow(keys + ['cost_amount', 'cost_currency'])
    for record in usage:
        cost = record['cost'] or {}
        writer.writerow([csv_cell(record[k]) for k in keys] + [csv_cell(cost.get(k)) for k in ('amount', 'currency')])
    return stream.getvalue()


def environment():
    try:
        revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL).decode().strip()
        dirty = bool(subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], stderr=subprocess.DEVNULL))
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    return dict(python=platform.python_version(), exporter_revision=revision, exporter_dirty=dirty,
                packages={name: importlib.metadata.version(name) for name in ('fastapi', 'uvicorn', 'httpx', 'pydantic', 'pydantic-settings', 'jsonschema')},
                files_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                              for name in ('pyproject.toml', 'uv.lock', 'contracts/schema.json', 'contracts/openapi.json')})


def render(evidence):
    run, summary = evidence['run'], evidence['summary']
    totals = summary['usage']['totals']
    mode = 'синтетический' if evidence['synthetic'] is True else 'реестр реальных этапов' if evidence['synthetic'] is False else 'не установлен'
    lines = ['# Измерения запуска', '', f"Запуск: {run['id']}, ревизия {run['revision']}, бриф v{run['brief_version']}.",
             f"Состояние: {run['status']}; качество: {run['quality']}; режим: {mode}.",
             f"Снято: {evidence['captured_at']}.", '',
             f"Активное время: {summary['active_seconds']} с; попытки модели: {summary['model_calls']}; инструментов: {summary['search_calls']}.",
             f"Записанные ошибки вызовов: {totals['errors']}; повторы: {totals['retries']}; попадания в кэш: {totals['cached_events']}.",
             f"Резервы без Usage: {summary['budget']['unrecorded_attempts']}; полнота учёта: {summary['accounting_complete']}.",
             f"Записанные ответы/выборы: {len(evidence['recorded_inputs'])}; использовано исправлений: {evidence['repairs_used']}.", '',
             '| Валюта | Сообщено сервисом | Оценено |', '|---|---:|---:|']
    for currency, values in totals['cost_by_currency'].items():
        lines.append(f"| {currency} | {values['reported']} | {values['estimated']} |")
    if not totals['cost_by_currency']:
        lines += ['', 'Известные суммы отсутствуют; это не доказательство бесплатности реальных вызовов.']
    lines += ['', f"Событий с неизвестной ценой: {totals['unknown_cost_events']}.", '',
              'Денежный резерв — оценка, а не предел списаний провайдера. Валюты не складываются.',
              'Время ожидания человека не включено в активное; после аварии неизвестный простой до восстановления учитывается консервативно.',
              'Записи ввода не доказывают участие человека: в demo их создаёт проверочный сценарий.',
              'Версия кода выполнения не записывалась в Run. Манифест описывает среду экспорта и сохранённые версии этапов.',
              'Работающие и приостановленные запуски представлены на момент наблюдения; последующие расходы в пакет не входят.',
              'Синтетический прогон не измеряет качество стратегии, живые тарифы или затраты на разработку.',
              'Перед передачей проверить названия моделей/инструментов и метки версий на закрытые сведения.']
    return '\n'.join(lines) + '\n'


def export_run(data_dir, run_id, destination):
    evidence = run_evidence(Storage(data_dir, readonly=True), run_id)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Export destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.export-', dir=destination.parent))
    try:
        files = {'metrics.json': json_text(evidence), 'usage.csv': usage_csv(evidence['usage']), 'README.md': render(evidence)}
        for name, text in files.items():
            (staging / name).write_text(text, encoding='utf-8')
        manifest = dict(format_version='1.0.0', run_id=run_id, captured_at=evidence['captured_at'],
                        export_environment=environment(),
                        files={name: dict(sha256=hashlib.sha256((staging / name).read_bytes()).hexdigest(),
                                         bytes=(staging / name).stat().st_size) for name in files})
        (staging / 'manifest.json').write_text(json_text(manifest), encoding='utf-8')
        # Reserve the final directory without replacing an earlier export, including concurrent exporters.
        destination.mkdir(mode=0o700)
        try:
            for path in staging.iterdir():
                os.replace(path, destination / path.name)
        except BaseException:
            shutil.rmtree(destination)
            raise
    finally:
        shutil.rmtree(staging)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True, help='Existing APP_DATA_DIR; .env is not read')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-dir', type=Path, required=True, help='New directory; existing files are never replaced')
    args = parser.parse_args(argv)
    try:
        path = export_run(args.data_dir, args.run_id, args.output_dir)
    except (OSError, ValueError, sqlite3.Error, StorageError):
        print('Export failed. Check the database version, run ID and a new writable output directory; no source contents were printed.')
        return 1
    print('Export saved: ' + str(path))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
