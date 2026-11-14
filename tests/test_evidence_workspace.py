"""Filtered selection, bounded questions, durable follow-ups and Word archives."""
import io
import json
import time
from copy import deepcopy
import pytest
from docx import Document
from langgraph.checkpoint.memory import InMemorySaver
from backend.app import evidence_agents as ea, evidence_workspace as ew, model, workflows as w
from backend.app.db import connect, dumps
from test_contracts import clients, create, run
from test_evidence_agents import scenario


def complete(x):
    output=ea.run_evidence(x['state'])
    with connect(True) as c:c.execute("UPDATE runs SET status='succeeded' WHERE id=?",(x['rid'],))
    return x['a'].get('/api/v1/artifacts/'+output['outputs'][-1]).json()


def test_selection_uses_saved_filters_not_fresh_retrieval(scenario,monkeypatch):
    x=scenario
    monkeypatch.setattr(w,'retrieve_cases',lambda *a:pytest.fail('must not run a fresh unfiltered search'))
    body={'goal':'分析证据','workflow':'evidence.gaps','evidence_selection':{'similar_version':1,'doc_ids':['J7','J2']}}
    result=x['a'].post('/api/v1/cases/'+x['cid']+'/requests',json=body,headers={'Idempotency-Key':'selected-filtered'})
    assert result.status_code==202,result.text
    snap=x['a'].get('/api/v1/runs/'+result.json()['id']).json()['snapshot']
    batch=ea.prepare_batch({**x['state'],'run_id':result.json()['id'],'snapshot':snap})
    assert batch['selected_doc_ids']==['J7','J2'] and batch['selection_mode']=='filtered_selected'
    assert snap['evidence_selection']['options']['mode']=='semantic'
    # The queued scope is immutable even if the user later changes the saved list.
    with connect(True) as c:c.execute("UPDATE similar_cases SET response=? WHERE case_id=?",(dumps({'items':[]}),x['cid']))
    assert ea.prepare_batch({**x['state'],'run_id':result.json()['id'],'snapshot':snap})['selected_doc_ids']==['J7','J2']


@pytest.mark.parametrize('ids',[[],['J1']*2,['J1','J2','J3','J4','J5','J6'],['foreign-case']])
def test_selection_api_rejects_invalid_scope(scenario,ids):
    x=scenario
    response=run(x['a'],x['cid'],key='bad-scope',goal='分析证据',workflow='evidence.gaps',evidence_selection={'similar_version':1,'doc_ids':ids})
    assert response.status_code==422


def test_selection_respects_removals_and_version_conflicts(scenario):
    x=scenario
    removed=x['a'].put('/api/v1/cases/'+x['cid']+'/similar-cases/J1/relevance',json={'version':1,'relevant':False})
    assert removed.status_code==200
    assert run(x['a'],x['cid'],key='old-list',goal='分析证据',workflow='evidence.gaps',evidence_selection={'similar_version':1,'doc_ids':['J2']}).status_code==409
    assert run(x['a'],x['cid'],key='removed-case',goal='分析证据',workflow='evidence.gaps',evidence_selection={'similar_version':2,'doc_ids':['J1']}).status_code==422
    response=run(x['a'],x['cid'],key='default-new-list',goal='分析证据',workflow='evidence.gaps')
    snap=x['a'].get('/api/v1/runs/'+response.json()['id']).json()['snapshot']
    assert snap['evidence_selection']['doc_ids']==['J2','J3','J4']
    assert snap['evidence_selection']['selection_mode']=='filtered_default'


@pytest.mark.parametrize('state',['missing','stale_materials','changed_filters','empty'])
def test_no_unfiltered_fallback(scenario,state):
    x=scenario
    with connect(True) as c:
        if state=='missing':c.execute('DELETE FROM similar_cases WHERE case_id=?',(x['cid'],))
        elif state=='stale_materials':c.execute('UPDATE similar_cases SET source_revision=0 WHERE case_id=?',(x['cid'],))
        elif state=='changed_filters':c.execute("UPDATE similar_cases SET options='{}' WHERE case_id=?",(x['cid'],))
        else:c.execute('UPDATE similar_cases SET response=? WHERE case_id=?',(dumps({'items':[]}),x['cid']))
    response=run(x['a'],x['cid'],key='no-fallback',goal='分析证据',workflow='evidence.gaps')
    assert response.status_code==(422 if state=='empty' else 409)


def test_report_separates_actual_materials_supplements_and_ten_questions(scenario,monkeypatch):
    x=scenario
    def generate(op,*args,**kwargs):
        value=x['generate'](op,*args,**kwargs)
        if ':evidence_synthesis:' in op:
            for i,item in enumerate(value['items']):item['verification_question']=f'请核实第{i+1}项材料原件在哪里？'
            value['unresolved']=[{'question':f'请核实补充问题{i+1}？','why_matters':f'明确第{i+1}项材料的核对范围。'} for i in range(6)]
        return value
    monkeypatch.setattr(ea,'generate',generate)
    artifact=complete(x);view=artifact['evidence_view']
    assert len(view['follow_up_questions'])==10
    assert len(view['existing_evidence'])==1 and view['existing_evidence'][0]['material_id']==x['mid']
    assert all(e['why_needed'] and e['evidence_needed'] and e['obtain_from'] for e in view['additional_evidence'])
    assert all(s.startswith('M') for e in view['existing_evidence'] for s in e['source_ids'])
    assert len(artifact['structured']['follow_up_questions'])==10
    assert len(view['follow_up_details'])==10
    assert [i['question'] for i in view['follow_up_details']]==view['follow_up_questions']
    assert all(0<len(i['question'])<=60 and 0<len(i['why_matters'])<=60 for i in view['follow_up_details'])
    legacy=deepcopy(artifact);legacy['structured'].pop('follow_up_questions');legacy['structured'].pop('follow_up_details');legacy['structured'].pop('existing_evidence')
    assert len(ew.evidence_view(legacy)['follow_up_questions'])<=10


def answer_run(x,artifact,monkeypatch,key='ask-one-001',bad_ref=False):
    response=run(x['a'],x['cid'],key=key,goal='为什么需要合同原件？',workflow='evidence.followup',evidence_artifact_id=artifact['id'])
    assert response.status_code==202,response.text
    rid=response.json()['id'];snap=x['a'].get('/api/v1/runs/'+rid).json()['snapshot']
    calls=[]
    def generate(op,system,payload,*args,**kwargs):
        calls.append(deepcopy(payload))
        return {'paragraphs':[{'text':'现有材料记载了约定交房时间，可进一步核对合同原件以确认约定的完整内容。','source_ids':['foreign' if bad_ref else 'M1']}],
            'limitations':['材料记载及证明力仍需结合原件核对。']}
    monkeypatch.setattr(model,'generate',generate)
    graph=w.build_graph(InMemorySaver())
    result=graph.invoke({'run_id':rid,'snapshot':snap,'plan':['evidence.followup'],'started':time.time()}, {'configurable':{'thread_id':rid}})
    with connect(True) as c:c.execute("UPDATE runs SET status='succeeded' WHERE id=?",(rid,))
    return x['a'].get('/api/v1/artifacts/'+result['outputs'][-1]).json(),calls


def test_followup_is_persisted_scoped_and_reuses_conversation(scenario,monkeypatch):
    x=scenario;artifact=complete(x)
    answer,calls=answer_run(x,artifact,monkeypatch)
    assert answer['kind']=='evidence_answer' and answer['structured']['evidence_artifact_id']==artifact['id']
    assert len(calls)==1 and calls[0]['evidence_summary'] and not calls[0]['conversation']
    second,calls=answer_run(x,artifact,monkeypatch,'ask-two-002')
    assert calls[0]['conversation'][0]['question']=='为什么需要合同原件？'
    persisted=x['a'].get('/api/v1/cases/'+x['cid']).json()
    assert len([a for a in persisted['artifacts'] if a['kind']=='evidence_answer'])==2
    assert all(r['evidence_artifact_id']==artifact['id'] for r in persisted['runs'] if r['plan']==['evidence.followup'])
    assert x['b'].get('/api/v1/artifacts/'+answer['id']).status_code==404


def test_followup_rejects_unknown_citation(scenario,monkeypatch):
    x=scenario;artifact=complete(x)
    with pytest.raises(ValueError,match='来源'):answer_run(x,artifact,monkeypatch,bad_ref=True)
    assert not [a for a in x['a'].get('/api/v1/cases/'+x['cid']).json()['artifacts'] if a['kind']=='evidence_answer']


def test_followup_scope_and_version_validation(scenario):
    x=scenario;artifact=complete(x)
    body={'goal':'为什么需要原件？','workflow':'evidence.followup','evidence_artifact_id':artifact['id']}
    assert x['b'].post('/api/v1/cases/'+x['cid']+'/requests',json=body,headers={'Idempotency-Key':'foreign-ask'}).status_code==404
    other,_=create(x['a'])
    assert run(x['a'],other,key='cross-ask',**body).status_code==422
    assert run(x['a'],x['cid'],key='missing-artifact',goal='继续追问',workflow='evidence.followup').status_code==422
    first=run(x['a'],x['cid'],key='first-pending',**body)
    assert first.status_code==202
    assert run(x['a'],x['cid'],key='second-pending',**body).status_code==409
    x['a'].post('/api/v1/cases/'+x['cid']+'/materials/text',json={'name':'新增材料','text':'新增的交付通知，需要核对。'})
    assert run(x['a'],x['cid'],key='stale-ask',**body).status_code==409


def export_reference(x,artifact,key='reference-one',include_answers=True):
    return x['a'].post('/api/v1/artifacts/'+artifact['id']+'/reference-documents',
        json={'content_hash':artifact['content_hash'],'include_answers':include_answers,'timezone':'America/New_York'},headers={'Idempotency-Key':key})


def test_word_archive_timestamp_content_idempotency_and_material_separation(scenario,monkeypatch,tmp_path):
    x=scenario;artifact=complete(x);answer_run(x,artifact,monkeypatch)
    before=x['a'].get('/api/v1/cases/'+x['cid']).json()
    response=export_reference(x,artifact)
    assert response.status_code==201,response.text
    record=response.json()
    assert record['created_at'] and record['name'].endswith('.docx') and record['answer_count']==0
    assert export_reference(x,artifact).json()['id']==record['id']
    assert export_reference(x,artifact,include_answers=False).status_code==409
    downloaded=x['a'].get('/api/v1/reference-documents/'+record['id']+'/download')
    assert downloaded.status_code==200 and 'wordprocessingml' in downloaded.headers['content-type']
    doc=Document(io.BytesIO(downloaded.content));text='\n'.join(p.text for p in doc.paragraphs)
    assert [p.text for p in doc.paragraphs if p.style.name=='Heading 1']==['已有证据与材料','建议补充或核对的证据','待核问题']
    assert len(doc.tables)==1
    assert [c.text for c in doc.tables[0].rows[0].cells]==['证据','获取渠道','为何需要']
    text+='\n'+'\n'.join(c.text for row in doc.tables[0].rows for c in row.cells)
    for expected in ['已有证据与材料','建议补充或核对的证据','为何需要','待核问题','为何重要','合同摘录','合同原件与付款凭证','本案当事人']:
        assert expected in text
    for excluded in ['逐案分析','证据追问记录','来源索引','分析范围说明','下一步','[M1]','生成时间','分析版本','为什么需要合同原件？']:
        assert excluded not in text
    after=x['a'].get('/api/v1/cases/'+x['cid']).json()
    assert after['revision']==before['revision'] and after['source_revision']==before['source_revision']
    assert after['materials']==before['materials'] and len(after['reference_documents'])==1
    second=export_reference(x,artifact,key='reference-two')
    assert second.status_code==201 and second.json()['name']!=record['name']
    assert x['b'].get('/api/v1/reference-documents/'+record['id']+'/download').status_code==404
    endpoint='/api/v1/artifacts/'+artifact['id']+'/reference-documents'
    assert x['a'].post(endpoint,json={'content_hash':artifact['content_hash']},headers={'Idempotency-Key':'csrf-test','X-CSRF-Token':''}).status_code==403
    x['a'].delete('/api/v1/materials/'+x['mid'])
    assert x['a'].get('/api/v1/reference-documents/'+record['id']+'/download').status_code==422
    assert not x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents'][0]['available']
    assert export_reference(x,artifact,key='withdrawn-reference').status_code==409


def test_word_without_analysis_or_invalid_timezone_is_rejected(scenario):
    x=scenario;artifact=complete(x)
    endpoint='/api/v1/artifacts/'+artifact['id']+'/reference-documents'
    assert x['a'].post(endpoint,json={'content_hash':'old'},headers={'Idempotency-Key':'hash-conflict'}).status_code==409
    assert x['a'].post(endpoint,json={'content_hash':artifact['content_hash'],'timezone':'wrong/timezone'},headers={'Idempotency-Key':'wrong-timezone'}).status_code==422


def test_legacy_compound_questions_are_split_and_capped():
    artifact={'sources':[],'structured':{'items':[{'issue':'通知','verification_question':'通知发出了吗？签收了吗？','next_step':'核对通知原件'}],
        'unresolved':['通知发出了吗?',*[f'补充问题{i}？' for i in range(12)]]}}
    view=ew.evidence_view(artifact)
    assert len(view['follow_up_questions'])==10
    assert view['follow_up_questions'][:2]==['通知发出了吗？','签收了吗？']
    assert all(q.count('？')+q.count('?')==1 for q in view['follow_up_questions'])


def test_legacy_questions_get_short_reasons_without_mutating_saved_content():
    artifact={'sources':[],'structured':{'items':[{'issue':'通知','verification_question':'通知发出了吗？签收了吗？',
        'why_needed':'核实通知是否到达。这里还有无需展示的详细分析。','next_step':'核对通知原件'}],
        'follow_up_questions':['已有材料只能提供线索。通知发出了吗？','签收了吗？','通知发出了吗?'], 'unresolved':[]}}
    saved=deepcopy(artifact)
    assert ew.evidence_view(artifact)['follow_up_details']==[
        {'question':'通知发出了吗？','why_matters':'核实通知是否到达。'},
        {'question':'签收了吗？','why_matters':'核实通知是否到达。'}]
    assert artifact==saved


def test_generated_questions_require_one_short_question_and_reason():
    from pydantic import ValidationError
    for invalid in [
        {'question':'通知发出了吗？签收了吗？','why_matters':'核实通知是否到达。'},
        {'question':'通知发出了吗？','why_matters':'核实通知是否到达。还有详细分析。'},
        {'question':'通知发出了吗？','why_matters':'核实'*31},
        {'question':'通知发出了吗？'},
    ]:
        with pytest.raises(ValidationError):ea.VerificationQuestion.model_validate(invalid)
