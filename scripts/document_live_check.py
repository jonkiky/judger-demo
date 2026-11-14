"""Real local-model document run; all case storage is isolated from user cases."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = Path(tempfile.mkdtemp(prefix="caiyan-document-live-"))
os.environ["CAIYAN_DATA_DIR"] = str(DATA)
(DATA/"corpus").mkdir()
(DATA/"corpus/release.json").write_text(json.dumps({"release_id": "document-live-fixture", "scope": "isolated document validation"}))
shutil.copyfile(ROOT/"data/internal.key", DATA/"internal.key")
(DATA/"internal.key").chmod(0o600)
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from backend.app.main import app
from backend.app import workflows as w
from backend.app.db import connect

out = ROOT/"evaluation/document-workspace"
out.mkdir(parents=True, exist_ok=True)
with TestClient(app) as client:
    credentials=json.loads((DATA/"local-credentials.json").read_text())
    client.headers['X-CSRF-Token']=client.post('/api/v1/auth/login',json=credentials).json()['csrf']
    case=client.post('/api/v1/cases',json={'title':'文书工作区验收（虚构）','claims':'整理合同日期及付款记载','position':'买方代理人'}).json()
    cid=case['id']
    for name,text in [('合同记录','买方张甲与卖方李乙于2026年1月5日签订合同，约定价款为人民币二十万元。'),('付款记录','张甲于2026年1月6日向李乙支付人民币十万元，剩余款项的实际支付情况未见记载。')]:
        assert client.post('/api/v1/cases/'+cid+'/materials/text',json={'name':name,'text':text}).status_code==201
    template=client.post('/api/v1/cases/'+cid+'/materials/text',json={'name':'情况说明模板.md','kind':'文书模板','text':'# 情况说明\n\n## 事实经过\n说明签约主体和日期。\n\n## 付款与待核事项\n列出已付金额，未载明的情况写待补充。'}).json()
    response=client.post('/api/v1/cases/'+cid+'/requests',json={'workflow':'document.generate','document_type':'client','document_template_id':template['id'],'goal':'按模板整理情况说明，保留材料中的金额和日期；不要计算或推断尚未记载的付款。'},headers={'Idempotency-Key':'document-live-validation'})
    assert response.status_code==202,response.text
    rid=response.json()['id'];run=client.get('/api/v1/runs/'+rid).json()
    print('Started isolated real-model draft',flush=True)
    state={'run_id':rid,'snapshot':run['snapshot'],'plan':run['plan'],'started':time.time()}
    result=w.build_graph(InMemorySaver()).invoke(state,{'configurable':{'thread_id':rid}})
    with connect(True) as c:c.execute("UPDATE runs SET status='succeeded' WHERE id=?",(rid,))
    artifact=client.get('/api/v1/artifacts/'+result['outputs'][-1]).json()
    saved=client.get('/api/v1/cases/'+cid).json()['reference_documents'][0]
    download=client.get('/api/v1/reference-documents/'+saved['id']+'/download')
    assert download.status_code==200
    (out/'model-draft.docx').write_bytes(download.content)
    (out/'model-draft.json').write_text(json.dumps(artifact,ensure_ascii=False,indent=2))
    with connect() as c:
        calls=[dict(row) for row in c.execute('SELECT operation_key,status,elapsed_ms FROM model_calls WHERE run_id=?',(rid,))]
        agents=[dict(row) for row in c.execute('SELECT material_id,status,attempt FROM document_agents WHERE run_id=?',(rid,))]
    summary={'checks':artifact['checks'],'readers':agents,'calls':calls,'file_bytes':len(download.content),'template_sections':run['snapshot']['document_template']['sections']}
    (out/'live-result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False),flush=True)
