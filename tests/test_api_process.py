"""HTTP acceptance with real API/worker processes and a fresh API process on restart."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

from scripts.try_api import run_demo

ROOT = Path(__file__).resolve().parents[1]


def test_http_demo_with_separate_worker_and_server_restart(tmp_path):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    environment = {k: v for k, v in os.environ.items() if not k.startswith('APP_')}
    environment.update(APP_HOST='127.0.0.1', APP_PORT=str(port), APP_DATA_DIR=str(tmp_path / 'state'),
                       APP_STAGE_FACTORY='demo', APP_WORKER_POLL_SECONDS='0.05', APP_LOG_LEVEL='warning')
    base_url = f'http://127.0.0.1:{port}'
    processes = []

    def launch(module):
        process = subprocess.Popen([sys.executable, '-E', '-m', module], cwd=ROOT, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def wait_server(client, process):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None, 'API process exited before becoming healthy'
            try:
                if client.get('/health').status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        raise TimeoutError('API did not start')

    try:
        server = launch('app')
        with httpx.Client(base_url=base_url, timeout=2, trust_env=False) as client:
            wait_server(client, server)
            worker = launch('app.worker')
            result = run_demo(base_url, tmp_path / 'documents')
            output_dir = Path(result['output_dir'])
            expected = json.loads((output_dir / 'snapshot.json').read_text())
            stop(worker)
            stop(server)
        server = launch('app')
        with httpx.Client(base_url=base_url, timeout=2, trust_env=False) as client:
            wait_server(client, server)
            response = client.get('/api/v1/runs/' + result['run_id'] + '/snapshot')
            assert response.status_code == 200 and response.json() == expected
            for exported, extension in zip(result['exports'], ('md', 'html')):
                response = client.get(exported['download_url'])
                assert response.status_code == 200
                assert response.content == (output_dir / ('report.' + extension)).read_bytes()
    finally:
        for process in processes:
            stop(process)
