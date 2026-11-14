"""Security and lifecycle contracts against real FastAPI and isolated SQLite storage."""
import io
import json
import sqlite3
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from backend.app import settings
from backend.app.db import init, connect, now, digest, dumps, password_hash
from backend.app.main import app
from backend.app.materials import material_ref, validate_material_ref, parse_file
from backend.app.workflows import route, validate_paragraphs

@pytest.fixture
def clients(tmp_path, monkeypatch):
    import backend.app.main as main
    monkeypatch.setattr(main, "login_attempts", {})
    monkeypatch.setattr(settings, "DATA", tmp_path)
    monkeypatch.setattr(settings, "DB", tmp_path/"workspace.sqlite3")
    monkeypatch.setattr(settings, "CORPUS", tmp_path/"corpus")
    settings.CORPUS.mkdir()
    (settings.CORPUS/"release.json").write_text(dumps({"release_id":"fixture","scope":"contract tests"}))
    init()
    with connect(True) as c:
        salt="12"*16
        c.execute("INSERT INTO users VALUES (?,?,?,?,?,?,0)", ("second","second","第二账号","lawyer",salt,password_hash("password",salt)))
    with TestClient(app) as a, TestClient(app) as b:
        credentials=json.loads((tmp_path/"local-credentials.json").read_text())
        a.headers['X-CSRF-Token']=a.post('/api/v1/auth/login',json=credentials).json()['csrf']
        b.headers['X-CSRF-Token']=b.post('/api/v1/auth/login',json={"username":"second","password":"password"}).json()['csrf']
        yield a,b

def create(client):
    c=client.post('/api/v1/cases',json={"title":"合同测试案件","claims":"核查逾期交付"}).json()
    m=client.post('/api/v1/cases/'+c['id']+'/materials/text',json={"name":"合同摘录","text":"买方陈某与开发商于2026年1月1日订约。约定2026年5月31日交房。"}).json()
    return c['id'],m['id']

def run(client,cid,key='contract-run-001',**overrides):
    return client.post('/api/v1/cases/'+cid+'/requests',json={"goal":"整理案情时间线",**overrides},headers={'Idempotency-Key':key})

def seed_artifact(cid,rid,mid):
    with connect(True) as c:
        m=dict(c.execute('select * from materials where id=?',(mid,)).fetchone())
        ref=material_ref(m,'约定2026年5月31日交房。');ref['id']='M1'
        revision=c.execute('select revision from cases where id=?',(cid,)).fetchone()[0]
        body='# 客户情况说明\n\n约定2026年5月31日交房。[M1]\n\n## 复核事项\n\n- 核对原件。'
        c.execute('INSERT INTO artifacts (id,case_id,run_id,kind,version,title,body,structured,sources,content_hash,checks,completeness,case_revision,created_at,operation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('test_art',cid,rid,'document',1,'客户情况说明',body,dumps({'unresolved':['核对原件。']}),dumps([ref]),digest(body),dumps({'passed':True}),'limited',revision,now(),'test-save'))
    return body

def test_login_csrf_and_logout(clients):
    a,b=clients
    assert a.post('/api/v1/cases',json={'title':'x'},headers={'X-CSRF-Token':''}).status_code==403
    assert a.post('/api/v1/cases',json={'title':'x'},headers={'Origin':'https://untrusted.example'}).status_code==403
    assert a.post('/api/v1/auth/logout').status_code==200
    assert a.get('/api/v1/cases').status_code==401

def test_case_material_run_isolation(clients):
    a,b=clients;cid,mid=create(a);rid=run(a,cid).json()['id']
    assert b.get('/api/v1/cases/'+cid).status_code==404
    assert b.get('/api/v1/materials/'+mid).status_code==404
    assert b.get('/api/v1/materials/'+mid+'/download').status_code==404
    assert b.get('/api/v1/runs/'+rid).status_code==404
    assert b.get('/api/v1/runs/'+rid+'/events').status_code==404
    assert b.post('/api/v1/runs/'+rid+'/cancel').status_code==404
    assert b.get('/api/v1/admin/configurations').status_code==403

def test_dashboard_case_summary_scope_and_preparation(clients):
    a,b=clients
    assert a.get('/api/v1/cases').json()==[]
    case=a.post('/api/v1/cases',json={'title':'工作台汇总测试'}).json()
    summary=a.get('/api/v1/cases').json()[0]
    assert summary['work_status']=='needs_materials' and summary['material_count']==0
    assert b.get('/api/v1/cases').json()==[]
    a.post('/api/v1/cases/'+case['id']+'/materials/text',json={'name':'合同','text':'待核查合同履行情况。'})
    with connect(True) as c:
        c.execute('INSERT INTO case_members(case_id,user_id) VALUES (?,?)',(case['id'],'second'))
    shared=b.get('/api/v1/cases').json()
    assert len(shared)==1 and shared[0]['work_status']=='ready_to_research'
    assert shared[0]['material_count']==1
    with connect(True) as c:
        c.execute('UPDATE materials SET withdrawn=1 WHERE case_id=?',(case['id'],))
    assert a.get('/api/v1/cases').json()[0]['work_status']=='needs_materials'

@pytest.mark.parametrize('status',['queued','running','waiting_input','succeeded','failed','cancelled'])
def test_dashboard_reports_current_task_status(clients,status):
    a,_=clients;cid,_=create(a);rid=run(a,cid).json()['id']
    with connect(True) as c:
        c.execute('UPDATE runs SET status=? WHERE id=?',(status,rid))
    summary=a.get('/api/v1/cases').json()[0]
    assert summary['work_status']==status
    assert summary['waiting_count']==int(status=='waiting_input')

def test_dashboard_attention_and_active_work_take_priority(clients):
    a,_=clients;cid,_=create(a);first=run(a,cid).json()['id']
    with connect(True) as c:
        c.execute("UPDATE runs SET status='waiting_input',created_at='2026-01-01T00:00:00' WHERE id=?",(first,))
    second=run(a,cid,key='dashboard-second').json()['id']
    with connect(True) as c:
        c.execute("UPDATE runs SET status='running',created_at='2026-01-02T00:00:00' WHERE id=?",(second,))
    assert a.get('/api/v1/cases').json()[0]['work_status']=='waiting_input'
    with connect(True) as c:
        c.execute("UPDATE runs SET status='failed',created_at='2026-01-03T00:00:00' WHERE id=?",(first,))
    assert a.get('/api/v1/cases').json()[0]['work_status']=='running'
    with connect(True) as c:
        c.execute("UPDATE runs SET status='queued' WHERE id=?",(second,))
    assert a.get('/api/v1/cases').json()[0]['work_status']=='queued'
    with connect(True) as c:
        c.execute("UPDATE runs SET status='succeeded',created_at='2026-01-04T00:00:00' WHERE id=?",(second,))
    assert a.get('/api/v1/cases').json()[0]['work_status']=='succeeded'

def test_dashboard_does_not_count_outdated_research_as_complete(clients):
    a,_=clients;cid,_=create(a);rid=run(a,cid).json()['id']
    with connect(True) as c:
        c.execute("UPDATE runs SET status='succeeded' WHERE id=?",(rid,))
    assert a.get('/api/v1/cases').json()[0]['work_status']=='succeeded'
    a.post('/api/v1/cases/'+cid+'/materials/text',json={'name':'补充材料','text':'新增事实，需要重新研究。'})
    assert a.get('/api/v1/cases').json()[0]['work_status']=='ready_to_research'

def test_idempotency_conflicts_and_queue_persistence(clients):
    a,_=clients;cid,_=create(a)
    r=run(a,cid);assert r.status_code==202
    assert run(a,cid).json()['id']==r.json()['id']
    assert run(a,cid,goal='分析证据缺口').status_code==409
    with connect() as c:
        assert c.execute('select count(*) from runs').fetchone()[0]==1
        assert c.execute('select count(*) from events').fetchone()[0]==1

def test_cross_case_material_scope_rejected(clients):
    a,_=clients;cid,_=create(a);_,other=create(a)
    assert run(a,cid,material_ids=[other]).status_code==422

def test_missing_and_partial_materials_never_silently_skipped(clients):
    a,_=clients;cid,mid=create(a)
    with connect(True) as c:c.execute("UPDATE materials SET status='partial' WHERE id=?",(mid,))
    assert run(a,cid).status_code==422
    assert run(a,cid,material_ids=[mid]).status_code==422

def test_version_conflict(clients):
    a,_=clients;cid,_=create(a)
    assert a.patch('/api/v1/cases/'+cid,json={'title':'changed'},headers={'If-Match':'1'}).status_code==409
    assert a.patch('/api/v1/cases/'+cid,json={'title':'changed'},headers={'If-Match':'2'}).status_code==200

def test_case_edit_persists_all_details_and_invalidates_previous_work(clients):
    a,b=clients;cid,mid=create(a);rid=run(a,cid).json()['id']
    seed_artifact(cid,rid,mid)
    before=a.get('/api/v1/cases/'+cid).json()
    values={'title':'修改后的案件名称','position':'被告代理律师','claims':'核查新提出的诉求','research_date':'2026-11-07'}
    headers={'If-Match':str(before['revision'])}
    assert b.patch('/api/v1/cases/'+cid,json=values,headers=headers).status_code==404
    updated=a.patch('/api/v1/cases/'+cid,json=values,headers=headers)
    assert updated.status_code==200
    persisted=a.get('/api/v1/cases/'+cid).json()
    assert all(persisted[key]==value for key,value in values.items())
    assert persisted['revision']==before['revision']+1
    assert persisted['artifacts'][0]['stale']
    assert persisted['runs'][0]['status']=='cancelled'
    assert a.get('/api/v1/cases').json()[0]['title']==values['title']
    assert a.patch('/api/v1/cases/'+cid,json={**values,'title':'过期修改'},headers=headers).status_code==409
    assert a.get('/api/v1/cases/'+cid).json()['title']==values['title']

def test_case_deletion_requires_owner_csrf_and_current_revision(clients):
    a,b=clients;cid,_=create(a)
    url='/api/v1/cases/'+cid
    revision=a.get(url).json()['revision']
    headers={'If-Match':str(revision)}
    assert b.delete(url,headers=headers).status_code==404
    with connect(True) as c:
        c.execute('INSERT INTO case_members(case_id,user_id) VALUES (?,?)',(cid,'second'))
    assert b.get(url).status_code==200
    assert b.delete(url,headers=headers).status_code==403
    assert a.delete(url,headers={**headers,'X-CSRF-Token':''}).status_code==403
    assert a.delete(url,headers={**headers,'Origin':'https://untrusted.example'}).status_code==403
    assert a.delete(url).status_code==422
    assert a.delete(url,headers={'If-Match':str(revision-1)}).status_code==409
    assert a.get(url).json()['revision']==revision
    with connect() as c:
        assert c.execute('SELECT deleted_at FROM cases WHERE id=?',(cid,)).fetchone()[0] is None
        assert not c.execute("SELECT 1 FROM audit WHERE action='case.delete' AND resource_id=?",(cid,)).fetchone()

@pytest.mark.parametrize('status',['queued','running','waiting_input','succeeded'])
def test_case_deletion_hides_resources_and_cancels_active_work(clients,status):
    a,b=clients;cid,mid=create(a);rid=run(a,cid).json()['id']
    seed_artifact(cid,rid,mid)
    other,other_material=create(a)
    with connect(True) as c:
        c.execute('UPDATE runs SET status=? WHERE id=?',(status,rid))
        c.execute('INSERT INTO case_members(case_id,user_id) VALUES (?,?)',(cid,'second'))
    revision=a.get('/api/v1/cases/'+cid).json()['revision']
    deleted=a.delete('/api/v1/cases/'+cid,headers={'If-Match':str(revision)})
    assert deleted.status_code==200 and deleted.json()=={'id':cid,'status':'deleted'}
    assert [item['id'] for item in a.get('/api/v1/cases').json()]==[other]
    assert b.get('/api/v1/cases').json()==[]
    for client in (a,b):
        for path in ('/cases/'+cid,'/cases/'+cid+'/similar-cases','/materials/'+mid,
                     '/materials/'+mid+'/download','/runs/'+rid,'/runs/'+rid+'/events',
                     '/runs/'+rid+'/evidence','/artifacts/test_art','/artifacts/test_art/export'):
            assert client.get('/api/v1'+path).status_code==404,path
    assert a.patch('/api/v1/cases/'+cid,json={'title':'恢复'},headers={'If-Match':str(revision+1)}).status_code==404
    assert a.post('/api/v1/cases/'+cid+'/materials/text',json={'name':'新材料','text':'内容'}).status_code==404
    assert run(a,cid,key='after-case-deletion').status_code==404
    assert a.post('/api/v1/runs/'+rid+'/resume',json={'answer':{'workflow':'case.timeline'}}).status_code==404
    assert a.delete('/api/v1/cases/'+cid,headers={'If-Match':str(revision)}).status_code==404
    assert a.get('/api/v1/materials/'+other_material).status_code==200
    with connect() as c:
        case=c.execute('SELECT * FROM cases WHERE id=?',(cid,)).fetchone()
        task=c.execute('SELECT status,cancel_requested FROM runs WHERE id=?',(rid,)).fetchone()
        assert case['deleted_at'] and case['revision']==revision+1
        assert task['status']==('succeeded' if status=='succeeded' else 'cancelled')
        assert task['cancel_requested']==(0 if status=='succeeded' else 1)
        assert c.execute("SELECT count(*) FROM audit WHERE action='case.delete' AND resource_id=?",(cid,)).fetchone()[0]==1
    # Existing data survives initialization; deleted cases must stay hidden after a restart.
    init()
    assert [item['id'] for item in a.get('/api/v1/cases').json()]==[other]

def test_new_material_cancels_old_run(clients):
    a,_=clients;cid,_=create(a);rid=run(a,cid).json()['id']
    a.post('/api/v1/cases/'+cid+'/materials/text',json={'name':'补充材料','text':'待核实收件人身份。'})
    assert a.get('/api/v1/runs/'+rid).json()['status']=='cancelled'

def test_export_requires_exact_review_and_scope(clients):
    a,b=clients;cid,mid=create(a);rid=run(a,cid).json()['id'];body=seed_artifact(cid,rid,mid)
    assert a.get('/api/v1/artifacts/test_art/export').status_code==409
    assert b.get('/api/v1/artifacts/test_art').status_code==404
    assert b.get('/api/v1/artifacts/test_art/export').status_code==404
    assert a.post('/api/v1/artifacts/test_art/reviews',json={'content_hash':'wrong','confirmed':True}).status_code==409
    assert a.post('/api/v1/artifacts/test_art/reviews',json={'content_hash':digest(body),'confirmed':True}).status_code==200
    exported=a.get('/api/v1/artifacts/test_art/export');assert exported.status_code==200
    assert '来源与版本附录' in exported.text
    a.post('/api/v1/cases/'+cid+'/materials/text',json={'name':'新证据','text':'开发商发出新的交付通知，待核对。'})
    assert a.get('/api/v1/artifacts/test_art/export').status_code==409

def test_edit_preserves_original_and_invalidates_review(clients):
    a,_=clients;cid,mid=create(a);rid=run(a,cid).json()['id'];body=seed_artifact(cid,rid,mid)
    a.post('/api/v1/artifacts/test_art/reviews',json={'content_hash':digest(body),'confirmed':True})
    edited=a.post('/api/v1/artifacts/test_art/versions',json={'body':body+'\n\n补充律师说明。','content_hash':digest(body)})
    assert edited.status_code==201 and edited.json()['review_status']=='pending'
    assert a.get('/api/v1/artifacts/test_art').json()['body']==body
    assert a.post('/api/v1/artifacts/test_art/versions',json={'body':body,'content_hash':digest(body)}).status_code==409

def test_unknown_citation_and_removed_limitations_block_review(clients):
    a,_=clients;cid,mid=create(a);rid=run(a,cid).json()['id'];body=seed_artifact(cid,rid,mid)
    edited=a.post('/api/v1/artifacts/test_art/versions',json={'body':body.replace('核对原件。','')+' [M999]','content_hash':digest(body)}).json()
    assert not edited['checks']['passed']
    assert a.post('/api/v1/artifacts/'+edited['id']+'/reviews',json={'content_hash':edited['content_hash'],'confirmed':True}).status_code==422

def test_rejected_semantics_block_export_until_new_version_reviewed(clients):
    a,_=clients;cid,mid=create(a);rid=run(a,cid).json()['id'];body=seed_artifact(cid,rid,mid)
    a.post('/api/v1/artifacts/test_art/reviews',json={'content_hash':digest(body),'confirmed':True})
    feedback=a.post('/api/v1/cases/'+cid+'/feedback',json={'target_id':'test_art','rating':'unsupported','note':'推论没有得到引文支持。'})
    assert feedback.status_code==201
    rejected=a.get('/api/v1/artifacts/test_art').json()
    assert rejected['checks']['semantic_review']=='rejected' and rejected['review_status']=='pending'
    assert a.get('/api/v1/artifacts/test_art/export').status_code==422
    revised=a.post('/api/v1/artifacts/test_art/versions',json={'body':body+'\n已修订为有条件结论。','content_hash':digest(body)}).json()
    assert revised['checks']['passed'] and revised['review_status']=='pending'
    assert a.get('/api/v1/artifacts/'+revised['id']+'/export').status_code==409

def test_statutory_article_must_be_supported_by_cited_source():
    from backend.app.workflows import article_numbers
    assert article_numbers('第一百八十八条、第五百六十四条、第563条')=={188,564,563}
    sources=[{'id':'L_civil_188','quote':'第一百八十八条 诉讼时效','name':'民法典'}]
    result={'sections':[{'heading':'期间','paragraphs':[{'text':'根据民法典第五百六十四条。','source_ids':['L_civil_188']}]}]}
    with pytest.raises(ValueError,match='法条条号'):validate_paragraphs(result,sources)

def test_failed_task_retry_keeps_snapshot_and_obeys_budget(clients):
    a,b=clients;cid,_=create(a);rid=run(a,cid).json()['id']
    with connect(True) as c:
        snapshot=c.execute('select snapshot from runs where id=?',(rid,)).fetchone()[0]
        c.execute("update runs set status='failed' where id=?",(rid,))
    assert b.post('/api/v1/runs/'+rid+'/retry').status_code==404
    assert a.post('/api/v1/runs/'+rid+'/retry').status_code==202
    with connect(True) as c:
        assert c.execute('select snapshot from runs where id=?',(rid,)).fetchone()[0]==snapshot
        c.execute("update runs set status='failed',recovery_count=2 where id=?",(rid,))
    assert a.post('/api/v1/runs/'+rid+'/retry').status_code==409

def test_withdrawn_source_hides_derived_body(clients):
    a,_=clients;cid,mid=create(a);rid=run(a,cid).json()['id'];seed_artifact(cid,rid,mid)
    assert a.delete('/api/v1/materials/'+mid).status_code==200
    assert a.get('/api/v1/materials/'+mid).status_code==404
    result=a.get('/api/v1/artifacts/test_art').json()
    assert '来源已撤回' in result['body'] and result['sources']==[]
    assert a.get('/api/v1/artifacts/test_art/export').status_code==409

def test_config_content_change_invalidates_test(clients):
    a,_=clients
    original=a.get('/api/v1/admin/configurations').json()[0]['content']
    created=a.post('/api/v1/admin/configurations/tests',json=original)
    assert created.status_code==202
    tid=created.json()['id']
    with connect(True) as c:c.execute("update config_tests set status='passed',result='{}' where id=?",(tid,))
    original['tone']='新文风'
    result=a.post('/api/v1/admin/configurations/publish',json={'content':original,'test_id':tid,'base_version':1,'reviewed':True})
    assert result.status_code==422

def test_resume_is_validated_and_consumed_once(clients):
    a,_=clients;cid,_=create(a);rid=run(a,cid).json()['id']
    with connect(True) as c:c.execute("UPDATE runs SET status='waiting_input',wait_payload=? WHERE id=?",(dumps({'kind':'case_selection','candidates':[{'document_id':'J1'}]}),rid))
    assert a.post('/api/v1/runs/'+rid+'/resume',json={'answer':{'doc_ids':['not-a-candidate']}}).status_code==422
    req={'answer':{'doc_ids':['J1']}}
    assert a.post('/api/v1/runs/'+rid+'/resume',json=req).status_code==202
    assert a.post('/api/v1/runs/'+rid+'/resume',json=req).json()['reused']
    assert a.post('/api/v1/runs/'+rid+'/resume',json={'answer':{'doc_ids':[]}}).status_code==409

def test_cancel_terminal_state_is_stable(clients):
    a,_=clients;cid,_=create(a);rid=run(a,cid).json()['id']
    assert a.post('/api/v1/runs/'+rid+'/cancel').json()['status']=='cancelled'
    assert a.post('/api/v1/runs/'+rid+'/cancel').json()['status']=='cancelled'

def test_document_parsers_and_exact_unicode_spans():
    text,loc,w,status=parse_file('证据.md','😀2026年6月，尚未交房。'.encode())
    assert status=='ready' and not w
    m={'id':'m','name':'n','version':1,'text':text,'text_hash':digest(text),'locations':loc}
    ref=material_ref(m,'2026年6月')
    assert ref['start']==1 and text[ref['start']:ref['end']]=='2026年6月'
    with pytest.raises(Exception):parse_file('file.pdf',b'not a PDF')
    with pytest.raises(Exception):parse_file('file.exe',b'x')
    with pytest.raises(ValueError):material_ref(m,'伪造引句')

def test_generic_intent_and_reference_contracts():
    assert route('起草二手房逾期过户纠纷民事起诉状')==['case.timeline','document.generate']
    assert route('分析租赁合同提前解除诉讼策略')==['case.timeline','case.assess']
    assert route('帮我看看')==[]
    with pytest.raises(ValueError):route('预测胜诉率')
    with pytest.raises(ValueError):validate_paragraphs({'sections':[{'heading':'观点','paragraphs':[{'text':'不存在','source_ids':['FAKE']}]}]},[{'id':'M1'}])
