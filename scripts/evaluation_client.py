"""Local acceptance-test client. Credentials stay in memory and are never printed."""
import json
import uuid
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]

def client():
    c = httpx.Client(base_url="http://127.0.0.1:8765/api/v1", trust_env=False, timeout=180)
    credentials = json.loads((ROOT/"data/local-credentials.json").read_text())
    r = c.post("/auth/login", json=credentials)
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c

def call(c, method, path, data=None):
    r = c.request(method, path, json=data, headers={"Idempotency-Key":str(uuid.uuid4())})
    r.raise_for_status()
    return r.json()

def save(name, data):
    target = ROOT/"evaluation"/name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2))

