"""Draft scope, isolated material readers, synthesis barrier, citations and versions."""
import io
import json
import time
from copy import deepcopy
import pytest
from docx import Document
from langgraph.checkpoint.memory import InMemorySaver
from backend.app import document_agents as da, workflows as w
from backend.app.db import connect, dumps
from test_contracts import clients, create, run


@pytest.fixture
def documents(clients, monkeypatch):
    a,b=clients
    cid,mid=create(a)
    second=a.post('/api/v1/cases/'+cid+'/materials/text',json={'name':'付款凭证','text':'买方于2026年1月2日支付房款人民币十万元。'}).json()['id']
    template=a.post('/api/v1/cases/'+cid+'/materials/text',json={'name':'指定文书模板.md','kind':'文书模板','text':'# 民事起诉状\n\n## 事实部分\n模板示例姓名甲某不属于案件事实。\n\n## 诉讼请求\n请说明诉求和对应材料。'}).json()['id']
    calls=[]
    def generate(op,system,payload,*args,**kwargs):
        calls.append((op,deepcopy(payload)))
        if ':document_read:' in op:
            return {'findings':[{'section':section,'summary':payload['spans'][0]['quote'],'source_ids':[payload['spans'][0]['id']]} for section in payload['required_sections']], 'missing':[]}
        assert ':document_compose:' in op
        rid=op.split(':')[0]
        with connect() as c:
            agents=da.progress(c,rid)
            assert len(agents)==2 and all(agent['status']=='succeeded' for agent in agents)
        return {'paragraphs':[{'text':s['quote'],'source_ids':[s['id']]} for s in payload['sources']], 'unresolved':['核对身份信息。']}
    monkeypatch.setattr(da,'generate',generate)
    return {'a':a,'b':b,'cid':cid,'mid':mid,'second':second,'template':template,'calls':calls,'generate':generate}


def queue(x, **kwargs):
    request=run(x['a'],x['cid'],key=kwargs.pop('key','document-test-001'),goal='按模板起草，保留付款记录并标注来源。',workflow='document.generate',document_type='complaint',document_template_id=x['template'],**kwargs)
    assert request.status_code==202,request.text
    rid=request.json()['id']
    saved=x['a'].get('/api/v1/runs/'+rid).json()
    return {'run_id':rid,'snapshot':saved['snapshot'],'plan':saved['plan'],'started':time.time(),'outputs':[],'sources':[]}


def complete(x,state):
    output=w.build_graph(InMemorySaver()).invoke(state,{'configurable':{'thread_id':state['run_id']}})
    with connect(True) as c:c.execute("UPDATE runs SET status='succeeded' WHERE id=?",(state['run_id'],))
    return x['a'].get('/api/v1/artifacts/'+output['outputs'][-1]).json()


def test_document_scope_template_and_instructions_are_frozen(documents):
    x=documents;state=queue(x,document_tasks={x['second']:'重点核对付款金额和时间'})
    assert state['plan']==['document.generate']
    snap=state['snapshot']
    assert set(snap['material_ids'])=={x['mid'],x['second']}
    assert snap['document_template']['sections']==['事实部分','诉讼请求']
    assert snap['document_template']['source']['role']=='template'
    assert snap['document_tasks'][x['second']]=='重点核对付款金额和时间'
    assert snap['model_call_budget']>=len(snap['material_ids'])+2


def test_each_reader_only_sees_its_material_then_all_reports_are_synthesized(documents):
    x=documents;state=queue(x)
    before=x['a'].get('/api/v1/cases/'+x['cid']).json()
    artifact=complete(x,state)
    after=x['a'].get('/api/v1/cases/'+x['cid']).json()
    assert artifact['kind']=='document' and artifact['checks']['passed']
    assert artifact['structured']['template']['id']==x['template']
    assert [s['heading'] for s in artifact['structured']['sections']]==['事实部分','诉讼请求']
    readers=[payload for op,payload in x['calls'] if ':document_read:' in op]
    assert len(readers)==2
    assert {r['material_name'] for r in readers}=={'合同摘录','付款凭证'}
    assert all(len({s['id'].split('_')[0] for s in r['spans']})==1 for r in readers)
    assert all('模板示例姓名甲某' not in dumps(r['spans']) for r in readers)
    composers=[payload for op,payload in x['calls'] if ':document_compose:' in op]
    assert all(len(p['all_material_reports'])==2 for p in composers)
    assert after['revision']==before['revision'] and len(after['materials'])==3
    assert len(after['reference_documents'])==1
    saved=after['reference_documents'][0]
    assert saved['kind']=='document' and saved['review_status']=='pending'
    response=x['a'].get('/api/v1/reference-documents/'+saved['id']+'/download')
    assert response.status_code==200
    doc=Document(io.BytesIO(response.content));text='\n'.join(p.text for p in doc.paragraphs)
    assert '事实部分' in text and '付款凭证' in text and '来源与版本' in text
    assert 'M1_1' in text and 'M2_1' in text
    # Recovery cannot duplicate a completed artifact or archive.
    da.run_document(state)
    assert len(x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents'])==1
    assert x['b'].get('/api/v1/runs/'+state['run_id']+'/document').status_code==404
    assert x['b'].get('/api/v1/reference-documents/'+saved['id']+'/download').status_code==404


def test_one_reader_failure_blocks_synthesis_and_retry_reuses_success(documents,monkeypatch):
    x=documents;state=queue(x)
    def fail(op,system,payload,*args,**kwargs):
        if ':document_read:' in op and payload['material_name']=='付款凭证': raise RuntimeError('模拟阅读失败')
        return x['generate'](op,system,payload,*args,**kwargs)
    monkeypatch.setattr(da,'generate',fail)
    with pytest.raises(ValueError,match='未全部完成'):da.run_document(state)
    assert not any(':document_compose:' in op for op,_ in x['calls'])
    assert not x['a'].get('/api/v1/cases/'+x['cid']).json()['artifacts']
    monkeypatch.setattr(da,'generate',x['generate'])
    artifact=complete(x,state)
    assert artifact['checks']['passed']
    assert len([p for op,p in x['calls'] if ':document_read:' in op and p['material_name']=='合同摘录'])==1


@pytest.mark.parametrize('where',['reader','orchestrator'])
def test_unknown_and_cross_material_citations_block_delivery(documents,monkeypatch,where):
    x=documents;state=queue(x)
    def bad(op,*args,**kwargs):
        result=x['generate'](op,*args,**kwargs)
        if where=='reader' and ':document_read:' in op:result['findings'][0]['source_ids']=['M999_1']
        if where=='orchestrator' and ':document_compose:' in op:result['paragraphs'][0]['source_ids']=['M999_1']
        return result
    monkeypatch.setattr(da,'generate',bad)
    with pytest.raises(ValueError):da.run_document(state)
    assert not x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents']


def test_manual_edit_and_instruction_revision_preserve_versions(documents):
    x=documents;first=complete(x,queue(x))
    assert x['a'].post('/api/v1/artifacts/'+first['id']+'/reviews',json={'content_hash':first['content_hash'],'confirmed':True}).status_code==200
    body=first['body'].replace('## 事实部分','## 事实部分\n\n人工增加的表述，待进一步核对。')
    edit=x['a'].post('/api/v1/artifacts/'+first['id']+'/versions',json={'content_hash':first['content_hash'],'body':body})
    assert edit.status_code==201,edit.text
    second=edit.json()
    assert second['version']==2 and second['review_status']=='pending'
    assert len(x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents'])==2
    conflict=run(x['a'],x['cid'],key='old-version-001',goal='继续修改文书',workflow='document.generate',document_artifact_id=first['id'],document_content_hash=first['content_hash'])
    assert conflict.status_code==409
    state=queue(x,key='revision-002',document_artifact_id=second['id'],document_content_hash=second['content_hash'])
    third=complete(x,state)
    assert third['parent_id']==second['id'] and third['version']==3
    assert len(third['structured']['conversation'])==2
    composers=[payload for op,payload in x['calls'] if op.startswith(state['run_id']+':document_compose:')]
    assert composers and all('人工增加的表述' in p['previous_draft'] for p in composers)
    assert x['a'].get('/api/v1/artifacts/'+first['id']).json()['body']==first['body']


def test_conflict_during_generation_and_cancellation_do_not_save(documents,monkeypatch):
    x=documents;first=complete(x,queue(x))
    state=queue(x,key='race-draft-001',document_artifact_id=first['id'],document_content_hash=first['content_hash'])
    edit=x['a'].post('/api/v1/artifacts/'+first['id']+'/versions',json={'content_hash':first['content_hash'],'body':first['body']+'\n人工修订。'})
    assert edit.status_code==201
    with pytest.raises(ValueError,match='已有新版本'):da.run_document(state)
    assert len(x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents'])==2


@pytest.mark.parametrize('withdraw',['mid','template'])
def test_withdrawal_revokes_draft_and_download(documents,withdraw):
    x=documents;state=queue(x);artifact=complete(x,state)
    file=x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents'][0]
    x['a'].delete('/api/v1/materials/'+x[withdraw])
    assert not x['a'].get('/api/v1/artifacts/'+artifact['id']).json()['checks']['passed']
    assert x['a'].get('/api/v1/reference-documents/'+file['id']+'/download').status_code==422
    if withdraw=='mid':
        reader=x['a'].get('/api/v1/runs/'+state['run_id']+'/document').json()['agents'][0]
        assert reader['result'] is None


def test_template_and_material_scope_rejects_cross_case_and_duplicates(documents):
    x=documents;other,foreign=create(x['a'])
    for extra in ({'document_template_id':foreign},{'material_ids':[x['mid'],x['mid']]},{'material_ids':[x['template']]},{'document_tasks':{foreign:'读取付款'}}):
        response=run(x['a'],x['cid'],key='bad-doc-'+str(len(dumps(extra))),goal='起草文书',workflow='document.generate',**extra)
        assert response.status_code==422,response.text
    assert x['b'].get('/api/v1/document-templates').status_code==200


def test_word_placeholder_template_retains_table_and_styles(documents):
    x=documents
    template=Document();template.add_paragraph('民事起诉状','Title')
    table=template.add_table(rows=1, cols=2);table.cell(0,0).text='事实部分';table.cell(0,1).text='{{事实部分}}'
    template.add_heading('请求事项',1);template.add_paragraph('{{诉讼请求}}')
    output=io.BytesIO();template.save(output)
    response=x['a'].post('/api/v1/cases/'+x['cid']+'/materials',data={'kind':'文书模板'},files={'file':('定制模板.docx',output.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
    assert response.status_code==201
    x['template']=response.json()['id']
    artifact=complete(x,queue(x))
    file=x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents'][0]
    result=x['a'].get('/api/v1/reference-documents/'+file['id']+'/download')
    doc=Document(io.BytesIO(result.content))
    assert len(doc.tables)==1 and 'M1_1' in doc.tables[0].cell(0,1).text
    assert '{{' not in doc.tables[0].cell(0,1).text
    assert doc.paragraphs[0].style.name=='Title'
    edited=x['a'].post('/api/v1/artifacts/'+artifact['id']+'/versions',json={'content_hash':artifact['content_hash'],'body':artifact['body'].replace('## 事实部分','## 事实部分\n\n补充核对意见。[M1_1]')})
    assert edited.status_code==201
    new_file=x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents'][0]
    updated=Document(io.BytesIO(x['a'].get('/api/v1/reference-documents/'+new_file['id']+'/download').content))
    assert len(updated.tables)==1 and '补充核对意见' in updated.tables[0].cell(0,1).text


def test_span_offsets_distinguish_repeated_text():
    material={'text':'同一段文字。'*200,'id':'mat_repeat','version':1,'name':'重复材料','text_hash':'hash','locations':'[]'}
    refs=da.spans(material,1)
    assert len(refs)>1
    assert refs[1]['start']==420 and refs[1]['quote']==material['text'][420:840]


def test_uploaded_templates_do_not_enter_timeline_or_similar_case_facts(documents):
    x=documents
    response=run(x['a'],x['cid'],key='timeline-no-template',workflow='case.timeline')
    assert response.status_code==202
    snapshot=x['a'].get('/api/v1/runs/'+response.json()['id']).json()['snapshot']
    assert x['template'] not in snapshot['material_ids']
    similar=x['a'].get('/api/v1/cases/'+x['cid']+'/similar-cases').json()
    assert '模板示例姓名甲某' not in similar['suggested_query']
