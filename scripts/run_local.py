"""One local command starts the HTTP UI and an independent durable worker."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo',action='store_true',help='Explicit synthetic stages; no model or search')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--data-dir',type=Path,default=Path('data'))
    args=parser.parse_args()
    env=dict(os.environ,APP_PORT=str(args.port),APP_HOST='127.0.0.1',APP_DATA_DIR=str(args.data_dir.resolve()))
    if args.demo:env['APP_STAGE_FACTORY']='demo'
    else:env.setdefault('APP_STAGE_FACTORY','app.agents:build_stages')
    processes=[];stop=False
    def stopped(*_):
        nonlocal stop
        stop=True
    for name in (signal.SIGINT,signal.SIGTERM):signal.signal(name,stopped)
    try:
        for module in ('app.worker','app'):
            processes.append(subprocess.Popen([sys.executable,'-E','-m',module],env=env))
        print(f'Интерфейс: http://127.0.0.1:{args.port} | '+('СИНТЕТИЧЕСКИЙ РЕЖИМ' if args.demo else 'РЕАЛЬНЫЕ АГЕНТЫ'),flush=True)
        while not stop:
            if any(p.poll() is not None for p in processes):break
            time.sleep(.25)
    finally:
        for p in processes:
            if p.poll() is None:p.terminate()
        for p in processes:
            try:p.wait(timeout=185)
            except subprocess.TimeoutExpired:p.kill();p.wait()
    return next((p.returncode for p in processes if p.returncode not in (0,-signal.SIGTERM)),0)


if __name__=='__main__':raise SystemExit(main())
