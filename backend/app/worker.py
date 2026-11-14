"""Single durable worker. OS lock proves previous executor exited before recovery."""
import fcntl
import json
import signal
import sqlite3
import time
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from . import settings
from .db import init, connect, now, dumps, event
from .model import generate
from .workflows import build_graph

stopping = False
def stop(*_):
    global stopping
    stopping = True

def process_config_test(row):
    content = json.loads(row["content"])
    try:
        result = generate("config-test:"+row["id"], "你是中文写作助手。按给定写作偏好组织一段客户说明，不新增事实。只返回 JSON {\"text\":\"说明\"}。必须保留通知仅寄出尚未证明签收、金额100万元。", {"preferences": content, "facts": "当事人陈述支付100万元。通知仅寄出，尚未取得签收记录。"}, max_tokens=700)
        text = result.get("text", "")
        passed = "100" in text and "签收" in text and any(s in text for s in ["尚未", "待", "未取得", "未确认"])
        with connect(True) as c:
            c.execute("UPDATE config_tests SET status=?,result=? WHERE id=?", ("passed" if passed else "failed", dumps({"output": result, "checks": {"amount_retained": "100" in text, "receipt_uncertain": passed}, "model_profile": json.loads((settings.DATA/"model-profile.json").read_text())}), row["id"]))
    except Exception as e:
        with connect(True) as c:
            c.execute("UPDATE config_tests SET status='failed',result=? WHERE id=?", (dumps({"error": str(e)}), row["id"]))

def main():
    init()
    lockfile = (settings.DATA/"worker.lock").open("a")
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with connect(True) as c:
        for row in c.execute("SELECT id,recovery_count FROM runs WHERE status='running'").fetchall():
            status = "queued" if row["recovery_count"] < 2 else "failed"
            c.execute("UPDATE runs SET status=?,recovery_count=recovery_count+1,updated_at=? WHERE id=?", (status, now(), row["id"]))
            event(c, row["id"], "run.recovered", {"status": status})
        c.execute("UPDATE config_tests SET status='queued' WHERE status='running'")
    with SqliteSaver.from_conn_string(str(settings.DATA/"checkpoints.sqlite3")) as checkpointer:
        graph = build_graph(checkpointer)
        while not stopping:
            with connect(True) as c:
                row = c.execute("SELECT * FROM runs WHERE status='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    c.execute("UPDATE runs SET status='running',updated_at=? WHERE id=?", (now(), row["id"]))
                    event(c, row["id"], "run.running", {"stage": "恢复执行" if row["recovery_count"] else "开始执行"})
                else:
                    test = c.execute("SELECT * FROM config_tests WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                    if test:
                        c.execute("UPDATE config_tests SET status='running' WHERE id=?", (test["id"],))
            if not row:
                if test:
                    process_config_test(test)
                else:
                    time.sleep(.5)
                continue
            cfg = {"configurable": {"thread_id": row["id"]}, "recursion_limit": 40}
            try:
                saved = graph.get_state(cfg)
                if saved.values:
                    pending_interrupt = any(t.interrupts for t in saved.tasks)
                    if row["recovery_count"] and not pending_interrupt:
                        # A bounded new attempt must not count time spent offline as inference.
                        graph.update_state(cfg, {"started": time.time()})
                    arg = Command(resume=json.loads(row["resume_payload"])) if pending_interrupt and row["resume_payload"] else None
                else:
                    arg = {"run_id": row["id"], "snapshot": json.loads(row["snapshot"]), "plan": json.loads(row["plan"]), "started": time.time()}
                result = graph.invoke(arg, cfg)
                saved = graph.get_state(cfg)
                interrupts = [i for t in saved.tasks for i in t.interrupts]
                with connect(True) as c:
                    current = c.execute("SELECT status,cancel_requested FROM runs WHERE id=?", (row["id"],)).fetchone()
                    if current["cancel_requested"]:
                        continue
                    if interrupts:
                        payload = interrupts[0].value
                        c.execute("UPDATE runs SET status='waiting_input',wait_payload=?,resume_payload=NULL,stage='等待您的选择',updated_at=? WHERE id=?", (dumps(payload), now(), row["id"]))
                        event(c, row["id"], "input.required", payload)
                    else:
                        c.execute("UPDATE runs SET status='succeeded',stage='已完成',updated_at=? WHERE id=?", (now(), row["id"]))
                        event(c, row["id"], "run.succeeded", {"outputs": result.get("outputs", [])})
            except Exception as exc:
                with connect(True) as c:
                    if not c.execute("SELECT cancel_requested FROM runs WHERE id=?", (row["id"],)).fetchone()[0]:
                        c.execute("UPDATE runs SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc)[:500], now(), row["id"]))
                        event(c, row["id"], "run.failed", {"error": str(exc)[:500]})

if __name__ == "__main__":
    main()
