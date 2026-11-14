import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from . import settings

def now():
    return datetime.now(timezone.utc).isoformat()

def uid(prefix="id"):
    return prefix + "_" + secrets.token_hex(12)

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else dumps(value)).encode()).hexdigest()

def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

@contextmanager
def connect(write=False):
    c = sqlite3.connect(settings.DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        if write:
            c.execute("BEGIN IMMEDIATE")
        yield c
        if write:
            c.commit()
    except BaseException:
        if write:
            c.rollback()
        raise
    finally:
        c.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,disabled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),title TEXT NOT NULL,position TEXT NOT NULL,claims TEXT NOT NULL,research_date TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS case_members(case_id TEXT REFERENCES cases(id),user_id TEXT REFERENCES users(id),PRIMARY KEY(case_id,user_id));
CREATE TABLE IF NOT EXISTS materials(id TEXT PRIMARY KEY,logical_id TEXT NOT NULL,case_id TEXT NOT NULL REFERENCES cases(id),version INTEGER NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,text_hash TEXT NOT NULL,file_hash TEXT NOT NULL,object_key TEXT,locations TEXT NOT NULL,status TEXT NOT NULL,warnings TEXT NOT NULL,active INTEGER DEFAULT 1,withdrawn INTEGER DEFAULT 0,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS material_case ON materials(case_id,active);
CREATE TABLE IF NOT EXISTS selections(case_id TEXT REFERENCES cases(id),doc_id TEXT NOT NULL,PRIMARY KEY(case_id,doc_id));
CREATE TABLE IF NOT EXISTS similar_cases(case_id TEXT PRIMARY KEY REFERENCES cases(id),version INTEGER NOT NULL,options TEXT NOT NULL,response TEXT,analyzed_options TEXT,source_revision INTEGER,analyzed_at TEXT,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS similar_case_exclusions(case_id TEXT NOT NULL REFERENCES cases(id),doc_id TEXT NOT NULL,item TEXT NOT NULL,reason TEXT NOT NULL DEFAULT 'irrelevant',user_id TEXT NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,PRIMARY KEY(case_id,doc_id));
CREATE TABLE IF NOT EXISTS configurations(id TEXT PRIMARY KEY,version INTEGER UNIQUE NOT NULL,content TEXT NOT NULL,content_hash TEXT NOT NULL,test_id TEXT,restored_from TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS config_tests(id TEXT PRIMARY KEY,content_hash TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,result TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,case_id TEXT REFERENCES cases(id),user_id TEXT NOT NULL REFERENCES users(id),goal TEXT NOT NULL,plan TEXT NOT NULL,status TEXT NOT NULL,snapshot TEXT NOT NULL,config_id TEXT NOT NULL,revision INTEGER NOT NULL,idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,stage TEXT NOT NULL,wait_payload TEXT,resume_payload TEXT,error TEXT,cancel_requested INTEGER DEFAULT 0,recovery_count INTEGER DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,idempotency_key));
CREATE TABLE IF NOT EXISTS events(run_id TEXT NOT NULL REFERENCES runs(id),seq INTEGER NOT NULL,type TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(run_id,seq));
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),run_id TEXT NOT NULL REFERENCES runs(id),kind TEXT NOT NULL,version INTEGER NOT NULL,parent_id TEXT,title TEXT NOT NULL,body TEXT NOT NULL,structured TEXT NOT NULL,sources TEXT NOT NULL,content_hash TEXT NOT NULL,checks TEXT NOT NULL,completeness TEXT NOT NULL,case_revision INTEGER NOT NULL,reviewed_by TEXT,reviewed_at TEXT,created_at TEXT NOT NULL,operation_key TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY,run_id TEXT,operation_key TEXT UNIQUE NOT NULL,request_hash TEXT NOT NULL,status TEXT NOT NULL,result TEXT,usage TEXT,elapsed_ms INTEGER,error TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_batches(run_id TEXT PRIMARY KEY REFERENCES runs(id),version TEXT NOT NULL,candidates TEXT NOT NULL,selected_doc_ids TEXT NOT NULL,retrieval TEXT NOT NULL,selection_mode TEXT NOT NULL,source_run_id TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,synthesis_attempt INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS evidence_agents(run_id TEXT NOT NULL REFERENCES evidence_batches(run_id),doc_id TEXT NOT NULL,rank INTEGER NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,input_hash TEXT,sources TEXT,coverage TEXT,result TEXT,error TEXT,started_at TEXT,finished_at TEXT,PRIMARY KEY(run_id,doc_id));
CREATE TABLE IF NOT EXISTS research_batches(run_id TEXT PRIMARY KEY REFERENCES runs(id),version TEXT NOT NULL,candidates TEXT NOT NULL,selected_doc_ids TEXT NOT NULL,selection_mode TEXT NOT NULL,status TEXT NOT NULL,synthesis_attempt INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_agents(run_id TEXT NOT NULL REFERENCES research_batches(run_id),doc_id TEXT NOT NULL,rank INTEGER NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,input_hash TEXT,sources TEXT,coverage TEXT,result TEXT,error TEXT,started_at TEXT,finished_at TEXT,PRIMARY KEY(run_id,doc_id));
CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL REFERENCES artifacts(id),user_id TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_agents(run_id TEXT NOT NULL REFERENCES runs(id),material_id TEXT NOT NULL REFERENCES materials(id),rank INTEGER NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,input_hash TEXT,sources TEXT,result TEXT,error TEXT,started_at TEXT,finished_at TEXT,PRIMARY KEY(run_id,material_id));
CREATE TABLE IF NOT EXISTS reference_documents(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),artifact_id TEXT NOT NULL REFERENCES artifacts(id),answer_ids TEXT NOT NULL,name TEXT NOT NULL,content BLOB NOT NULL,content_hash TEXT NOT NULL,user_id TEXT NOT NULL,created_at TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,UNIQUE(user_id,idempotency_key));
CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),user_id TEXT NOT NULL,target_id TEXT NOT NULL,rating TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,user_id TEXT,action TEXT NOT NULL,resource_id TEXT NOT NULL,created_at TEXT NOT NULL);
"""

DEFAULT_CONFIG = {
    "audience": "客户", "tone": "清晰、审慎、易懂", "length": "标准",
    "prompt": "围绕已核验材料组织说明，区分材料记载与研究建议，保留待核实事项。",
    "skill": "先交代资料范围，再说明已知情况与下一步。不得预测胜诉率，不得将公共判决当作本案证据。",
    "opening": "以下说明依据当前提供的材料整理，供进一步沟通与核实。",
    "model": settings.MODEL_ALIAS,
}

def init():
    settings.DATA.mkdir(parents=True, exist_ok=True)
    (settings.DATA / "uploads").mkdir(exist_ok=True)
    with connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        # Separate input revisions from changes to selected research judgments.
        if "source_revision" not in {r[1] for r in c.execute("PRAGMA table_info(cases)")}:
            c.execute("ALTER TABLE cases ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 1")
            c.commit()
        if "deleted_at" not in {r[1] for r in c.execute("PRAGMA table_info(cases)")}:
            c.execute("ALTER TABLE cases ADD COLUMN deleted_at TEXT")
            c.commit()
        if "deleted_at" not in {r[1] for r in c.execute("PRAGMA table_info(reference_documents)")}:
            c.execute("ALTER TABLE reference_documents ADD COLUMN deleted_at TEXT")
            c.commit()
    if not (settings.DATA / "internal.key").exists():
        p = settings.DATA / "internal.key"
        p.write_text(secrets.token_urlsafe(40))
        p.chmod(0o600)
    with connect(True) as c:
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            password = secrets.token_urlsafe(12)
            salt = secrets.token_hex(16)
            c.execute("INSERT INTO users VALUES (?,?,?,?,?,?,0)", ("local_admin", "admin", "本机律师", "admin", salt, password_hash(password, salt)))
            p = settings.DATA / "local-credentials.json"
            p.write_text(dumps({"username": "admin", "password": password}))
            p.chmod(0o600)
        if not c.execute("SELECT 1 FROM configurations LIMIT 1").fetchone():
            c.execute("INSERT INTO configurations VALUES (?,?,?,?,?,?,?)", ("config_initial", 1, dumps(DEFAULT_CONFIG), digest(DEFAULT_CONFIG), None, None, now(),))

def event(c, run_id, kind, payload):
    seq = c.execute("SELECT coalesce(max(seq),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
    c.execute("INSERT INTO events VALUES (?,?,?,?,?)", (run_id, seq, kind, dumps(payload), now()))

def audit(c, user_id, action, resource_id):
    c.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (uid("audit"), user_id, action, resource_id, now()))

def row_dict(row):
    return dict(row) if row else None
