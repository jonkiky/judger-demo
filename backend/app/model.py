import json
import time
import httpx
from . import settings
from .db import connect, uid, now, dumps, digest

def service(method, path, payload=None, base=None, timeout=300):
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        r = client.request(method, (base or settings.SEARCH_URL)+path, json=payload, headers={"X-Internal-Key": settings.internal_key()})
        if not r.is_success:
            try:
                detail = r.json().get("detail", "本地服务请求失败")
            except ValueError:
                detail = "本地服务返回错误"
            raise RuntimeError(str(detail))
        return r.json()

def generate(operation_key, system, payload, run_id=None, max_tokens=1800, output_schema=None):
    request = {"model": settings.MODEL_ALIAS, "messages": [{"role": "system", "content": system}, {"role": "user", "content": dumps(payload)}], "max_tokens": max_tokens}
    if output_schema:
        request["output_schema"] = output_schema
    hashed = digest(request)
    retrying = False
    with connect(True) as c:
        existing = c.execute("SELECT * FROM model_calls WHERE operation_key=?", (operation_key,)).fetchone()
        if existing:
            if existing["request_hash"] != hashed:
                raise ValueError("模型调用幂等键与请求内容不一致")
            if existing["status"] == "succeeded":
                return json.loads(existing["result"])
            # An interrupted HTTP request may have reached the model. One recovery retry only.
            if existing["status"] in ("retried", "retry_failed"):
                raise RuntimeError("模型调用结果仍不确定，请新建任务。")
            retrying = True
            c.execute("UPDATE model_calls SET status='retried' WHERE operation_key=?", (operation_key,))
        else:
            limit = 12
            if run_id:
                row = c.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
                if row:
                    limit = json.loads(row["snapshot"]).get("model_call_budget", 12)
                if c.execute("SELECT 1 FROM evidence_batches WHERE run_id=?", (run_id,)).fetchone():
                    limit = max(limit, 32)
                if c.execute("SELECT count(*) FROM model_calls WHERE run_id=?", (run_id,)).fetchone()[0] >= limit:
                    raise RuntimeError(f"本次任务已达 {limit} 次模型调用预算，请缩小范围。")
            c.execute("INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?)", (uid("call"), run_id, operation_key, hashed, "running", None, None, None, None, now()))
    started = time.monotonic()
    try:
        # Two independent requests can share the local model's queue. Include queue
        # time in the HTTP deadline; each actual inference still has a 240s limit.
        response = service("POST", "/v1/chat/completions", request, settings.GATEWAY,
                           timeout=240 * settings.EVIDENCE_CONCURRENCY + 60)
        content = response["choices"][0]["message"]["content"]
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("模型输出必须为 JSON 对象")
        with connect(True) as c:
            c.execute("UPDATE model_calls SET status='succeeded',result=?,usage=?,elapsed_ms=? WHERE operation_key=?", (dumps(value), dumps(response.get("usage")), round((time.monotonic()-started)*1000), operation_key))
        return value
    except Exception as exc:
        with connect(True) as c:
            c.execute("UPDATE model_calls SET status=?,error=?,elapsed_ms=? WHERE operation_key=?", ("retry_failed" if retrying else "failed", str(exc)[:300], round((time.monotonic()-started)*1000), operation_key))
        raise
