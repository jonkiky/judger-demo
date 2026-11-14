#!/usr/bin/env python3
"""Launch the isolated local stack. Ctrl+C stops only the children it created."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from backend.app import settings
from backend.app.db import init

def main():
    init()
    if not (settings.CORPUS/"release.json").exists():
        subprocess.run([sys.executable, str(ROOT/"scripts/prepare_corpus.py")], check=True)
    subprocess.run([sys.executable, str(ROOT/"scripts/prepare_filters.py")], check=True)
    if not (ROOT/"frontend/dist/index.html").exists():
        raise SystemExit("Build frontend first: npm --prefix frontend ci && npm --prefix frontend run build")
    for port in [8765,8766,8767]:
        with socket.socket() as s:
            try:
                # A recently stopped HTTP server can leave TIME_WAIT sockets.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                s.listen(1)
            except OSError:
                raise SystemExit(f"Port {port} is in use; stop the owning POC process before starting another.")
    logs = settings.DATA/"logs"
    logs.mkdir(exist_ok=True)
    env = {**os.environ,"LITELLM_LOCAL_MODEL_COST_MAP":"True","DO_NOT_TRACK":"1","LANGSMITH_TRACING":"false","ANONYMIZED_TELEMETRY":"False","PYTHONUNBUFFERED":"1"}
    children, handles = [], []
    def launch(name, args):
        handle = (logs/(name+".log")).open("a")
        handles.append(handle)
        child = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
        children.append(child)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        launch("gateway", ["-m","uvicorn","backend.app.gateway:app","--host","127.0.0.1","--port","8766","--no-access-log"])
        launch("search", ["-m","uvicorn","backend.app.search_service:app","--host","127.0.0.1","--port","8767","--no-access-log"])
        launch("api", ["-m","uvicorn","backend.app.main:app","--host","127.0.0.1","--port","8765","--no-access-log"])
        launch("worker", ["-m","backend.app.worker"])
        print("裁研 POC: http://127.0.0.1:8765", flush=True)
        print("Login credentials: "+str(settings.DATA/"local-credentials.json"), flush=True)
        print("Local services are starting. Logs: "+str(logs), flush=True)
        (settings.DATA/"processes.json").write_text(__import__('json').dumps({"supervisor":os.getpid(),"children":[c.pid for c in children]}))
        while all(c.poll() is None for c in children):
            time.sleep(1)
        raise RuntimeError("A service stopped; inspect data/logs before restarting.")
    except KeyboardInterrupt:
        print("Stopping local POC services…", flush=True)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for handle in handles:
            handle.close()

if __name__ == "__main__":
    main()
