"""Run the new UI against an isolated copy of the requested example case.

Search/model services remain local and real. All new tasks, answers and Word
references are written to a temporary workspace, never the user's case.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from backend.app import settings
from backend.app.db import init, connect, password_hash

source=settings.DATA
preview=Path(tempfile.mkdtemp(prefix='caiyan-evidence-preview-'))
settings.DATA=preview
settings.DB=preview/'workspace.sqlite3'
settings.CORPUS=preview/'corpus'
init()
(preview/'corpus').mkdir(exist_ok=True)
shutil.copyfile(source/'corpus/release.json',preview/'corpus/release.json')
shutil.copyfile(source/'internal.key',preview/'internal.key')
(preview/'internal.key').chmod(0o600)
cid='case_425879ebba3aa759ac7e4a9b'
original=sqlite3.connect(source/'workspace.sqlite3');original.row_factory=sqlite3.Row
with connect(True) as c:
    # Isolated test credentials, not the user's account password.
    c.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?',('34'*16,password_hash('evidence-preview-2026','34'*16),'local_admin'))
    case=dict(original.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone())
    case['title']='证据流程验收副本'
    tables=[('configurations','SELECT * FROM configurations',()),('cases','SELECT * FROM cases WHERE id=?',(cid,)),
        ('materials','SELECT * FROM materials WHERE case_id=?',(cid,)),('similar_cases','SELECT * FROM similar_cases WHERE case_id=?',(cid,)),
        ('similar_case_exclusions','SELECT * FROM similar_case_exclusions WHERE case_id=?',(cid,)),
        ('runs','SELECT * FROM runs WHERE case_id=?',(cid,)),('artifacts','SELECT * FROM artifacts WHERE case_id=?',(cid,)),
        ('evidence_batches','SELECT * FROM evidence_batches WHERE run_id IN (SELECT id FROM runs WHERE case_id=?)',(cid,)),
        ('evidence_agents','SELECT * FROM evidence_agents WHERE run_id IN (SELECT id FROM runs WHERE case_id=?)',(cid,))]
    for table,sql,args in tables:
        for row in original.execute(sql,args):
            values=dict(row)
            if table=='cases':values=case
            columns=list(values)
            c.execute('INSERT OR REPLACE INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',list(values.values()))
    # No source task is resumed automatically in this isolated preview.
    c.execute("UPDATE runs SET status='cancelled',cancel_requested=1 WHERE status IN ('queued','running','waiting_input')")
original.close()
env={**os.environ,'CAIYAN_DATA_DIR':str(preview),'PYTHONUNBUFFERED':'1'}
metadata={'data_dir':str(preview),'url':'http://127.0.0.1:8768/cases/'+cid+'/evidence'}
(root/'evaluation/evidence-workspace-preview.json').write_text(json.dumps(metadata,indent=2))
api=subprocess.Popen([sys.executable,'-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8768','--no-access-log'],cwd=root,env=env)
worker=subprocess.Popen([sys.executable,'-m','backend.app.worker'],cwd=root,env=env)
print(json.dumps(metadata),flush=True)
try:
    api.wait()
finally:
    worker.terminate();api.terminate()
    worker.wait(timeout=10)
