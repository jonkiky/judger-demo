"""Orchestration/authorization tests. Synthetic judgments stay in this fixture."""
import json
import threading
import time
from copy import deepcopy
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from backend.app import workflows as w, evidence_agents as ea
from backend.app.db import connect, digest, dumps, uid, now
from backend.app.materials import material_ref
from test_contracts import clients, create, run


@pytest.fixture
def scenario(clients, monkeypatch):
    a,b=clients
    cid,mid=create(a)
    documents={}
    candidates=[]
    for i in range(1,8):
        body=f'第{i}案：原告提交房屋买卖合同和付款凭证。本院查明，双方约定交房期限。本院认为，合同及付款凭证可相互印证，予以采信。判决如下：履行交付义务。'
        doc={'id':f'J{i}','title':f'第{i}案房屋买卖争议','case_number':f'(2025)测试{i}号',
             'court':'测试法院','judgment_date':'2025-06-01','cause':'房屋买卖合同纠纷','content':body,'sha256':digest(body),'release_id':'fixture'}
        documents[doc['id']]=doc
        candidates.append({k:v for k,v in {**doc,'document_id':doc['id'],'rank':i,'retrieval_score':1/i}.items() if k not in ('content','sha256','id')})
    retrieval=[{'query':'房屋买卖 逾期交付','retrieval_mode':'hybrid','warnings':[],'release_id':'fixture'}]
    from backend.app.contracts import SimilarCaseInput
    from backend.app.similar_cases import save_similar
    with connect(True) as c:
        revision=c.execute('SELECT source_revision FROM cases WHERE id=?',(cid,)).fetchone()[0]
        save_similar(c,cid,SimilarCaseInput(),response={'items':candidates,'release_id':'fixture','retrieval_mode':'semantic'},source_revision=revision)
    rid=run(a,cid,goal='分析证据',workflow='evidence.gaps',evidence_selection={'similar_version':1,'doc_ids':['J1','J2','J3','J4','J5']}).json()['id']
    snapshot=a.get('/api/v1/runs/'+rid).json()['snapshot']
    # A saved general-research selection must not override this evidence scope.
    snapshot['selected_doc_ids']=['not-an-evidence-candidate']
    with connect(True) as c:
        c.execute("UPDATE runs SET status='running' WHERE id=?",(rid,))
        material=dict(c.execute('SELECT * FROM materials WHERE id=?',(mid,)).fetchone())
    ref=material_ref(material,material['text']);ref['id']='M1'
    state={'run_id':rid,'snapshot':snapshot,'plan':['case.timeline','evidence.gaps'],
           'facts':[{'id':'M1','quote':ref['quote']}],'sources':[ref],'outputs':[],'started':time.time()}
    candidates=snapshot['evidence_selection']['candidates']
    def retrieve(*args,**kwargs):return {'candidates':deepcopy(candidates),'retrieval':retrieval}
    def service(method,path,*args,**kwargs):return deepcopy(documents[path.rsplit('/',1)[-1]])
    monkeypatch.setattr(w,'retrieve_cases',retrieve)
    monkeypatch.setattr(w,'service',service)
    monkeypatch.setattr(ea,'service',service)
    calls=[];lock=threading.Lock();barrier=threading.Barrier(2,timeout=5)
    activity={'current':0,'peak':0,'started':0}
    def generate(op,system,payload,*args,**kwargs):
        with lock:calls.append((op,deepcopy(payload)))
        if ':evidence_case:' in op:
            assert set(payload)=={'judgment','coverage','sources','evidence_names'}
            assert all(s['case_number']==payload['judgment']['case_number'] for s in payload['sources'])
            with lock:
                activity['current']+=1;activity['started']+=1
                number=activity['started'];activity['peak']=max(activity['peak'],activity['current'])
            try:
                if number<=2:barrier.wait()
                time.sleep(.01)
                return {'findings':[{'evidence':payload['evidence_names'][0],'fact':'约定交房期限及付款情况',
                    'treatment':'采信','reasoning':'判决记载可以相互印证','source_ids':[payload['sources'][0]['id']]}],
                    'limits':['本案证据之间的关系应如何结合判决原文核对？']}
            finally:
                with lock:activity['current']-=1
        assert ':evidence_synthesis:' in op
        with connect() as c:
            agents=c.execute('SELECT status FROM evidence_agents WHERE run_id=?',(rid,)).fetchall()
            assert len(agents)==len(payload['case_reports']) and all(r[0]=='succeeded' for r in agents)
        return {'items':[{'issue':f'类案{i+1}所涉及的合同与付款记载','finding_ids':[r['findings'][0]['id']],
             'material_source_ids':['M1'],'verification_question':'本案的合同约定、付款与交付事实分别有哪些原始材料可以核对？',
             'why_matters':'核实合同约定与实际履行是否一致。',
             'evidence_needed':'合同原件与付款凭证','why_needed':'核实合同约定与实际履行情况。','obtain_from':'本案当事人',
             'next_step':'向本案当事人核对合同原件和付款凭证，以核实约定及履行情况。'} for i,r in enumerate(payload['case_reports'])],
             'unresolved':[{'question':'本案证据原件及其真实性应如何核对？','why_matters':'确认现有材料是否完整可靠。'}]}
    monkeypatch.setattr(ea,'generate',generate)
    return dict(a=a,b=b,cid=cid,mid=mid,rid=rid,state=state,documents=documents,candidates=candidates,calls=calls,generate=generate,activity=activity)


def test_selected_five_isolated_calls_and_join(scenario):
    x=scenario
    result=ea.run_evidence(x['state'])
    calls=x['calls'];specialists=[p for op,p in calls if ':evidence_case:' in op]
    assert sorted(p['judgment']['id'] for p in specialists)==['J1','J2','J3','J4','J5']
    assert len([op for op,p in calls if ':evidence_synthesis:' in op])==1
    assert x['activity']['peak']==2
    artifact=x['a'].get('/api/v1/artifacts/'+result['outputs'][-1]).json()
    assert artifact['structured']['multi_agent']['total']==5
    progress=x['a'].get('/api/v1/runs/'+x['rid']+'/evidence').json()['batch']
    assert progress['selection_mode']=='filtered_selected' and progress['completed']==5
    # Re-entering a completed graph node is idempotent.
    assert ea.run_evidence(x['state'])==result and len(calls)==6


def test_synthesis_uses_evidence_types_without_foreign_facts(scenario):
    x=scenario
    result=ea.run_evidence(x['state'])
    payload=next(p for op,p in x['calls'] if ':evidence_synthesis:' in op)
    for report in payload['case_reports']:
        assert set(report)=={'doc_id','case_number','findings'}
        for finding in report['findings']:
            assert set(finding)=={'id','evidence_types','treatment'}
            assert finding['evidence_types']==['合同、协议或债权凭据','款项往来及收付凭证']
    artifact=x['a'].get('/api/v1/artifacts/'+result['outputs'][-1]).json()
    assert artifact['structured']['multi_agent']['case_reports'][0]['findings'][0]['evidence']
    report={'doc_id':'foreign','case_number':'(2024)测试1号','findings':[
        {'id':'E1_1','evidence':'《星河房屋转让协议》','fact':'王甲支付999万元','reasoning':'某法院认定的其他交易事实','treatment':'仅记载'}]}
    model_input=dumps(ea.synthesis_case_reports([report]))
    assert all(text not in model_input for text in ['星河','王甲','999','其他交易事实'])


def test_graph_finishes_evidence_without_user_selection(scenario,monkeypatch):
    x=scenario
    monkeypatch.setattr(w,'timeline',lambda state:{'facts':x['state']['facts'],'sources':x['state']['sources'],'outputs':[]})
    graph=w.build_graph(InMemorySaver());config={'configurable':{'thread_id':x['rid']}}
    result=graph.invoke(x['state'],config)
    assert result['outputs'] and not graph.get_state(config).tasks
    assert x['a'].get('/api/v1/runs/'+x['rid']+'/evidence').json()['batch']['completed']==5


def test_failed_case_blocks_synthesis_and_retry_reuses_successes(scenario,monkeypatch):
    x=scenario;failed=False
    def generate(op,*args,**kwargs):
        nonlocal failed
        if ':evidence_case:J3:' in op and not failed:
            failed=True;raise RuntimeError('simulated provider failure')
        return x['generate'](op,*args,**kwargs)
    monkeypatch.setattr(ea,'generate',generate)
    with pytest.raises(ValueError,match='4/5'):ea.run_evidence(x['state'])
    assert not any(':evidence_synthesis:' in op for op,p in x['calls'])
    with connect() as c:assert c.execute("SELECT count(*) FROM artifacts WHERE kind='evidence'").fetchone()[0]==0
    ea.run_evidence(x['state'])
    assert len([op for op,p in x['calls'] if ':evidence_case:' in op])==5
    with connect() as c:
        attempts=dict(c.execute('SELECT doc_id,attempt FROM evidence_agents WHERE run_id=?',(x['rid'],)))
    assert attempts=={'J1':1,'J2':1,'J3':2,'J4':1,'J5':1}


def test_incomplete_synthesis_retries_only_orchestrator(scenario,monkeypatch):
    x=scenario;first=True
    def generate(op,*args,**kwargs):
        nonlocal first
        result=x['generate'](op,*args,**kwargs)
        if ':evidence_synthesis:' in op and first:
            first=False;result['items'].pop()
        return result
    monkeypatch.setattr(ea,'generate',generate)
    with pytest.raises(ValueError,match='遗漏'):ea.run_evidence(x['state'])
    ea.run_evidence(x['state'])
    assert len([op for op,p in x['calls'] if ':evidence_case:' in op])==5
    assert [op.rsplit(':',1)[-1] for op,p in x['calls'] if ':evidence_synthesis:' in op]==['1','2']


@pytest.mark.parametrize('fault',[None,'missing','wrong_case'])
def test_synthesis_requires_a_grounded_task_for_each_selected_case(scenario,monkeypatch,fault):
    x=scenario
    def generate(op,system,payload,*args,**kwargs):
        result=x['generate'](op,system,payload,*args,**kwargs)
        if ':evidence_synthesis:' not in op:return result
        coverage=payload['required_case_coverage']
        schema=kwargs['output_schema']['properties']['evidence_by_case']
        assert schema['required']==list(coverage) and len(coverage)==5
        entries={key:item for key,item in zip(coverage,result['items'])}
        for key,value in coverage.items():
            assert schema['properties'][key]['properties']['finding_ids']['items']['enum']==value['finding_ids']
        if fault=='missing':entries.pop('case_5')
        if fault=='wrong_case':entries['case_5']['finding_ids']=entries['case_1']['finding_ids']
        return {'evidence_by_case':entries,'unresolved':result['unresolved']}
    monkeypatch.setattr(ea,'generate',generate)
    if fault:
        with pytest.raises(ValueError,match='遗漏|其他案例'):ea.run_evidence(x['state'])
        assert not [a for a in x['a'].get('/api/v1/cases/'+x['cid']).json()['artifacts'] if a['kind']=='evidence']
    else:
        result=ea.run_evidence(x['state'])
        artifact=x['a'].get('/api/v1/artifacts/'+result['outputs'][-1]).json()
        assert len(artifact['structured']['items'])==5


def test_recovery_revalidates_saved_response_without_another_llm_request(scenario,monkeypatch):
    x=scenario;original=ea.validate_case_result;first=True
    def generate(op,*args,**kwargs):
        result=x['generate'](op,*args,**kwargs)
        if ':evidence_case:J3:' in op:
            with connect(True) as c:
                c.execute('INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (uid('call'),x['rid'],op,'fixture','succeeded',dumps(result),'{}',10,None,now()))
        return result
    def validate(raw,sources,rank):
        nonlocal first
        if rank==3 and first:
            first=False;raise ValueError('temporary source resolver failure')
        return original(raw,sources,rank)
    monkeypatch.setattr(ea,'generate',generate);monkeypatch.setattr(ea,'validate_case_result',validate)
    with pytest.raises(ValueError,match='4/5'):ea.run_evidence(x['state'])
    ea.run_evidence(x['state'])
    assert len([op for op,p in x['calls'] if ':evidence_case:' in op])==5
    with connect() as c:
        assert c.execute("SELECT attempt FROM evidence_agents WHERE run_id=? AND doc_id='J3'",(x['rid'],)).fetchone()[0]==1
        assert any(json.loads(r[0]).get('revalidated_response') for r in c.execute("SELECT payload FROM events WHERE run_id=? AND type='evidence.agent.started'",(x['rid'],)))


def test_cross_case_reference_is_rejected(scenario,monkeypatch):
    x=scenario
    def generate(op,*args,**kwargs):
        result=x['generate'](op,*args,**kwargs)
        if ':evidence_case:J1:' in op:result['findings'][0]['source_ids']=['J5_1']
        return result
    monkeypatch.setattr(ea,'generate',generate)
    with pytest.raises(ValueError,match='另一个案件'):ea.run_evidence(x['state'])
    assert not any(':evidence_synthesis:' in op for op,p in x['calls'])


def test_cancellation_never_delivers_synthesis(scenario,monkeypatch):
    x=scenario
    def generate(op,*args,**kwargs):
        with connect(True) as c:c.execute("UPDATE runs SET cancel_requested=1,status='cancelled' WHERE id=?",(x['rid'],))
        return {'findings':[],'limits':['cancelled']}
    monkeypatch.setattr(ea,'generate',generate)
    with pytest.raises(ValueError):ea.run_evidence(x['state'])
    with connect() as c:
        assert c.execute('SELECT status FROM evidence_batches WHERE run_id=?',(x['rid'],)).fetchone()[0]=='cancelled'
        assert c.execute("SELECT count(*) FROM artifacts WHERE kind='evidence'").fetchone()[0]==0


def test_fewer_real_candidates_are_disclosed(scenario):
    x=scenario;x['candidates'][:]=x['candidates'][:2]
    x['state']['snapshot']['evidence_selection']['doc_ids']=['J1','J2']
    result=ea.run_evidence(x['state'])
    artifact=x['a'].get('/api/v1/artifacts/'+result['outputs'][-1]).json()
    assert artifact['structured']['multi_agent']['total']==2
    assert any('仅分析 2' in s for s in artifact['structured']['unresolved'])


def test_zero_candidates_never_fabricates_cases(scenario):
    x=scenario;x['candidates'].clear()
    x['state']['snapshot']['evidence_selection']['doc_ids']=[]
    with pytest.raises(ValueError,match='没有检索到'):ea.run_evidence(x['state'])
    assert x['calls']==[]


def test_duplicate_case_numbers_do_not_consume_two_slots(scenario):
    from backend.app.evidence_workspace import selection_snapshot
    x=scenario
    with connect(True) as c:
        row=c.execute('SELECT response FROM similar_cases WHERE case_id=?',(x['cid'],)).fetchone()
        response=json.loads(row[0]);response['items'][1]['case_number']=response['items'][0]['case_number']
        c.execute('UPDATE similar_cases SET response=? WHERE case_id=?',(dumps(response),x['cid']))
        case=dict(c.execute('SELECT * FROM cases WHERE id=?',(x['cid'],)).fetchone())
        selected=selection_snapshot(c,case)
    assert selected['doc_ids']==['J1','J3','J4']


def test_long_judgment_exact_windows_and_partial_coverage(scenario):
    doc=deepcopy(scenario['documents']['J1'])
    doc['content']='开头材料。'*2000+'本院认为，购房合同与银行流水可以证明付款情况。'+'后续文字。'*2000
    doc['sha256']=digest(doc['content'])
    refs,coverage=ea.judgment_spans(doc,1)
    assert coverage['partial'] and coverage['included_characters']<=6500
    assert any('本院认为' in ref['quote'] for ref in refs)
    assert all(doc['content'][s['start']:s['end']]==s['quote'] and s['doc_id']==doc['id'] for s in refs)


def test_evidence_names_are_verbatim_and_unknown_names_rejected(scenario):
    refs,_=ea.judgment_spans(scenario['documents']['J1'],1)
    names=ea.evidence_names(refs)
    assert names and all(any(name in s['quote'] for s in refs) for name in names)
    assert ea.evidence_names([{'quote':'法院准许撤诉。'}])==['原文未载具体证据名称']
    raw={'findings':[{'evidence':'虚构鉴定报告','fact':'付款情况','reasoning':'待核','treatment':'无法确认','source_ids':['J1_1']}],'limits':['原文应如何核对？']}
    with pytest.raises(ValueError,match='不是当前判决'):ea.validate_case_result(raw,refs,1)


def test_literal_name_citation_is_resolved_even_when_reasoning_cites_later_span(scenario):
    refs,_=ea.judgment_spans(scenario['documents']['J1'],1)
    later={**refs[0],'id':'J1_2','quote':'法院结合履行情况作出认定。'}
    refs.append(later)
    raw={'findings':[{'evidence':ea.evidence_names(refs)[0],'fact':'约定履行情况','reasoning':'原文记载履行事实。',
        'treatment':'仅记载','source_ids':['J1_2']}],'limits':['完整证据应如何核对？']}
    result=ea.validate_case_result(raw,refs,1)
    assert result['findings'][0]['source_ids']==['J1_1','J1_2']
    assert result['findings'][0]['evidence_source_id']=='J1_1'


def test_retry_rejects_changed_evidence_protocol(scenario):
    x=scenario;ea.prepare_batch(x['state'])
    with connect(True) as c:
        c.execute("UPDATE runs SET status='failed' WHERE id=?",(x['rid'],))
        c.execute("UPDATE evidence_batches SET version='previous-protocol' WHERE run_id=?",(x['rid'],))
    response=x['a'].post('/api/v1/runs/'+x['rid']+'/retry')
    assert response.status_code==409 and '流程已更新' in response.json()['detail']


def test_queued_snapshot_protocol_change_stops_before_any_call(scenario):
    x=scenario;x['state']['snapshot']['evidence_policy']['version']='previous-protocol'
    with pytest.raises(ValueError,match='流程版本已变化'):ea.run_evidence(x['state'])
    assert x['calls']==[]


def test_admission_requires_explicit_literal_evidence_ruling(scenario):
    refs,_=ea.judgment_spans(scenario['documents']['J1'],1)
    evidence='《房屋他项权证》'
    claim='本院认为，因尚未办理抵押登记，对优先受偿权的请求不予支持。'
    refs[0]['quote']=f'合同约定办理{evidence}。{claim}'
    raw={'findings':[{'evidence':evidence,'fact':'用于核对登记情况','reasoning':'裁判讨论抵押登记。',
        'treatment':'不采信','treatment_quote':claim,'source_ids':['J1_1']}],
        'limits':['证据名称在原文中属于约定还是实际提交的材料？']}
    finding=ea.validate_case_result(raw,refs,1)['findings'][0]
    assert finding['treatment']=='无法确认' and finding['admission_note'] and not finding['treatment_quote']
    # Even a plausible admission statement must actually occur in cited text.
    explicit=f'本院对{evidence}不予采信。'
    raw['findings'][0]['treatment_quote']=explicit
    assert ea.validate_case_result(raw,refs,1)['findings'][0]['treatment']=='无法确认'
    refs[0]['quote']+='\n'+explicit
    finding=ea.validate_case_result(raw,refs,1)['findings'][0]
    assert finding['treatment']=='不采信' and finding['treatment_quote']==explicit
    accepted=f'本院对{evidence}予以采信。'
    refs[0]['quote']+='\n'+accepted
    raw['findings'][0].update(treatment='采信',treatment_quote=accepted)
    assert ea.validate_case_result(raw,refs,1)['findings'][0]['treatment']=='采信'


def test_single_case_limits_cannot_assert_missing_facts(scenario):
    refs,_=ea.judgment_spans(scenario['documents']['J1'],1)
    raw={'findings':[{'evidence':ea.evidence_names(refs)[0],'fact':'约定履行情况','reasoning':'原文记载履行事实。',
        'treatment':'仅记载','source_ids':['J1_1']}],'limits':['判决未记载付款情况。']}
    with pytest.raises(ValueError):ea.validate_case_result(raw,refs,1)


def test_review_selection_api_scope_limits_and_snapshot(scenario):
    x=scenario;ea.prepare_batch(x['state'])
    with connect(True) as c:c.execute("UPDATE runs SET status='succeeded' WHERE id=?",(x['rid'],))
    endpoint='/api/v1/cases/'+x['cid']+'/requests'
    request={'goal':'重新分析证据','workflow':'evidence.gaps','evidence_review':{'run_id':x['rid'],'doc_ids':['J2','J3','J4','J5','J6']}}
    assert x['b'].get('/api/v1/runs/'+x['rid']+'/evidence').status_code==404
    assert x['b'].post(endpoint,json=request,headers={'Idempotency-Key':'foreign-review'}).status_code==404
    for ids in [[],['J1']*2,['J'+str(i) for i in range(1,7)],['not-retrieved']]:
        assert x['a'].post(endpoint,json={**request,'evidence_review':{'run_id':x['rid'],'doc_ids':ids}},headers={'Idempotency-Key':'invalid-review'}).status_code==422
    created=x['a'].post(endpoint,json=request,headers={'Idempotency-Key':'review-evidence-001'})
    assert created.status_code==202
    again=x['a'].post(endpoint,json=request,headers={'Idempotency-Key':'review-evidence-001'})
    assert again.json()['id']==created.json()['id']
    snap=x['a'].get('/api/v1/runs/'+created.json()['id']).json()['snapshot']
    assert snap['evidence_review']['doc_ids']==['J2','J3','J4','J5','J6'] and snap['material_ids']==[x['mid']]
    assert snap['model_call_budget']==32
    new_state={**x['state'],'run_id':created.json()['id'],'snapshot':snap}
    batch=ea.prepare_batch(new_state)
    assert batch['selection_mode']=='reviewed' and batch['selected_doc_ids']==['J2','J3','J4','J5','J6']
    assert batch['source_run_id']==x['rid']


def test_review_rejects_other_case_and_changed_materials(scenario):
    x=scenario;ea.prepare_batch(x['state'])
    with connect(True) as c:c.execute("UPDATE runs SET status='succeeded' WHERE id=?",(x['rid'],))
    request={'goal':'重新分析证据','workflow':'evidence.gaps','evidence_review':{'run_id':x['rid'],'doc_ids':['J1']}}
    cid,_=create(x['a'])
    assert x['a'].post('/api/v1/cases/'+cid+'/requests',json=request,headers={'Idempotency-Key':'other-case-review'}).status_code==422
    x['a'].post('/api/v1/cases/'+x['cid']+'/materials/text',json={'name':'新材料','text':'新的交房通知，真实性待核。'})
    assert x['a'].post('/api/v1/cases/'+x['cid']+'/requests',json=request,headers={'Idempotency-Key':'changed-case-review'}).status_code==409
