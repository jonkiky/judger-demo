"""Persistence, input invalidation and concurrent analysis against isolated storage."""
from copy import deepcopy
import pytest
from backend.app import main
from backend.app.db import connect
from backend.app.contracts import SimilarCaseInput
from backend.app.materials import store_material
from backend.app.similar_cases import save_similar, set_relevance
from test_contracts import clients, create


@pytest.fixture
def search(monkeypatch):
    calls = []
    def service(method, path, payload=None):
        if method == "GET":
            return {"id": "judgment-1"}
        calls.append(deepcopy(payload))
        return {"items": [{"document_id": "judgment-1", "title": "测试裁判", "quote": "测试原文", "rank": 1}],
                "applied_filters": payload, "retrieval_mode": payload["mode"], "warnings": [], "degraded": False,
                "elapsed_ms": 10, "candidate_count": 1, "release_id": "fixture", "scope": "test"}
    monkeypatch.setattr(main, "service", service)
    return calls


def endpoint(cid):
    return f"/api/v1/cases/{cid}/similar-cases"


def analyze(a, cid, options=None, version=0):
    r = a.post(endpoint(cid)+"/analyze", json={"options": options or {}, "version": version})
    assert r.status_code == 200, r.text
    return r.json()


def test_defaults_only_search_after_explicit_analysis(clients, search):
    a, _ = clients
    cid, _ = create(a)
    initial = a.get(endpoint(cid)).json()
    assert initial["options"]["mode"] == "semantic"
    assert initial["response"] is None and initial["status"] == "not_analyzed"
    assert "核查逾期交付" in initial["suggested_query"]
    assert "约定2026年5月31日交房" in initial["suggested_query"]
    assert not search
    options = {"filters": {"province": ["a"*64]}, "date_from": "2025-01-01", "limit": 5}
    saved = a.put(endpoint(cid), json={"options": options}).json()
    assert saved["version"] == 1 and saved["response"] is None and not search
    result = analyze(a, cid, saved["options"], 1)
    assert search[0]["mode"] == "semantic" and search[0]["filters"] == options["filters"]
    assert search[0]["query"] == initial["suggested_query"]
    assert not result["needs_analysis"] and result["count"] == 1
    # A fresh read gets the actual saved response, not another search.
    assert a.get(endpoint(cid)).json() == result and len(search) == 1
    with connect() as c:
        assert c.execute("SELECT response FROM similar_cases WHERE case_id=?", (cid,)).fetchone()[0]
    assert a.get('/api/v1/cases/'+cid).json()['similar_cases']['status'] == 'ready'
    assert a.get('/api/v1/cases').json()[0]['similar_cases']['count'] == 1


@pytest.mark.parametrize('change', ['add', 'replace', 'withdraw', 'title', 'position', 'claims', 'research_date'])
def test_every_material_and_overview_change_marks_saved_results_stale(clients, search, change):
    a, _ = clients
    cid, mid = create(a)
    result = analyze(a, cid)
    if change == 'add':
        r = a.post('/api/v1/cases/'+cid+'/materials/text', json={'name':'新材料', 'text':'新事实'})
    elif change == 'replace':
        r = a.post('/api/v1/materials/'+mid+'/versions', json={'name':'修改材料', 'text':'修改后的事实'})
    elif change == 'withdraw':
        r = a.delete('/api/v1/materials/'+mid)
    else:
        case = a.get('/api/v1/cases/'+cid).json()
        fields = {k:case[k] for k in ['title','position','claims','research_date']}
        fields[change] = '2030-01-01' if change == 'research_date' else '修改后的内容'
        r = a.patch('/api/v1/cases/'+cid, json=fields, headers={'If-Match':str(case['revision'])})
    assert r.is_success, r.text
    stale = a.get(endpoint(cid)).json()
    assert stale['stale'] and stale['status'] == 'stale'
    assert stale['response'] == result['response'] and len(search) == 1
    assert a.get('/api/v1/cases').json()[0]['similar_cases']['needs_analysis']
    rerun = analyze(a, cid, stale['options'], stale['version'])
    assert not rerun['needs_analysis'] and len(search) == 2


def test_noop_overview_and_selected_judgments_do_not_invalidate(clients, search):
    a, _ = clients
    cid, _ = create(a)
    analyze(a, cid)
    before = a.get('/api/v1/cases/'+cid).json()
    fields = {k:before[k] for k in ['title','position','claims','research_date']}
    r = a.patch('/api/v1/cases/'+cid, json=fields, headers={'If-Match':str(before['revision'])})
    assert r.json()['revision'] == before['revision']
    assert a.put('/api/v1/cases/'+cid+'/research-selections',json={'doc_ids':['judgment-1']}).is_success
    assert not a.get(endpoint(cid)).json()['stale']


def test_filter_edits_preserve_results_and_can_be_restored(clients, search):
    a, _ = clients
    cid, _ = create(a)
    initial = analyze(a, cid)
    options = {**initial['options'], 'query':'解除合同退款', 'filters':{'city':['b'*64]}}
    edited = a.put(endpoint(cid),json={'options':options,'version':initial['version']}).json()
    assert edited['options_changed'] and not edited['stale']
    assert edited['response'] == initial['response'] and len(search) == 1
    assert a.get(endpoint(cid)).json()['options'] == options
    restored = a.put(endpoint(cid),json={'options':initial['analyzed_options'],'version':edited['version']}).json()
    assert not restored['needs_analysis']
    assert a.put(endpoint(cid),json={'options':options,'version':0}).status_code == 409


def test_analysis_failure_preserves_last_success_and_saved_filters(clients, search, monkeypatch):
    a, _ = clients
    cid, _ = create(a)
    initial = analyze(a, cid)
    def fail(*args, **kwargs):
        raise RuntimeError('语义检索服务不可用')
    monkeypatch.setattr(main, 'service', fail)
    r = a.post(endpoint(cid)+'/analyze',json={'options':{'query':'变更条件'},'version':initial['version']})
    assert r.status_code == 503
    assert a.get(endpoint(cid)).json() == initial


def test_changes_during_analysis_cannot_mark_old_inputs_current(clients, search, monkeypatch):
    a, _ = clients
    cid, _ = create(a)
    service = main.service
    def change_material(*args, **kwargs):
        with connect(True) as c:
            store_material(c, cid, '分析期间新增', '新的事实', '测试材料')
        return service(*args, **kwargs)
    monkeypatch.setattr(main, 'service', change_material)
    result = analyze(a, cid)
    assert result['stale'] and result['response']
    assert '新的事实' not in result['response']['applied_filters']['query']
    assert '新的事实' in result['suggested_query']


def test_concurrent_options_save_is_not_overwritten_by_slow_analysis(clients, search, monkeypatch):
    a, _ = clients
    cid, _ = create(a)
    initial = analyze(a, cid)
    service = main.service
    def concurrent_save(*args, **kwargs):
        with connect(True) as c:
            save_similar(c,cid,SimilarCaseInput(version=initial['version'],options={'query':'另一个页面的条件'}))
        return service(*args, **kwargs)
    monkeypatch.setattr(main,'service',concurrent_save)
    r = a.post(endpoint(cid)+'/analyze',json={'options':{},'version':initial['version']})
    assert r.status_code == 409
    latest = a.get(endpoint(cid)).json()
    assert latest['options']['query'] == '另一个页面的条件'
    assert latest['response'] == initial['response']


def test_empty_results_are_saved_as_a_completed_analysis(clients, monkeypatch):
    a, _ = clients
    cid, _ = create(a)
    monkeypatch.setattr(main,'service',lambda method,path,payload:{'items':[],'applied_filters':payload,'warnings':[],'retrieval_mode':'semantic'})
    result = analyze(a, cid)
    assert result['status'] == 'ready' and result['analyzed_at'] and result['count'] == 0
    assert a.get(endpoint(cid)).json()['response']['items'] == []


def test_saved_analysis_and_options_are_case_scoped_and_validated(clients, search):
    a, b = clients
    cid, _ = create(a)
    for method,path in [('GET',endpoint(cid)),('PUT',endpoint(cid)),('POST',endpoint(cid)+'/analyze')]:
        r = b.request(method,path,**({'json':{}} if method!='GET' else {}))
        assert r.status_code == 404
    assert not search
    assert a.put(endpoint(cid),json={},headers={'X-CSRF-Token':''}).status_code == 403
    for options in [{'query':'字'},{'filters':{'city':['not-a-value-id']}},{'date_from':'2026-02-01','date_to':'2026-01-01'}]:
        assert a.put(endpoint(cid),json={'options':options}).status_code == 422


def test_generated_query_is_bounded_and_reports_incomplete_materials(clients, search):
    a, _ = clients
    cid, _ = create(a)
    with connect(True) as c:
        for i in range(22):
            store_material(c, cid, '长材料'+str(i), '正文很长'*3000, '测试材料', status='partial')
    state = a.get(endpoint(cid)).json()
    assert len(state['suggested_query']) <= 1500
    assert any('20 份' in w for w in state['query_warnings'])
    assert any('尚未完整解析' in w for w in state['query_warnings'])


def relevance(a, cid, version, relevant=False, doc_id='judgment-1'):
    return a.put(endpoint(cid)+'/'+doc_id+'/relevance',json={'version':version,'relevant':relevant})


def test_irrelevant_case_is_persisted_and_counts_update_without_changing_original(clients, search):
    a, _ = clients
    cid, _ = create(a)
    original = analyze(a, cid)
    response = relevance(a, cid, original['version'])
    assert response.status_code == 200, response.text
    excluded = response.json()
    assert excluded['count'] == 0 and excluded['response']['items'] == []
    assert excluded['excluded_count'] == excluded['excluded_from_current_count'] == 1
    assert excluded['excluded_items'][0]['reason'] == 'irrelevant'
    assert excluded['excluded_items'][0]['item'] == original['response']['items'][0]
    assert excluded['excluded_items'][0]['in_current_results']
    assert not excluded['needs_analysis']
    assert a.get(endpoint(cid)).json() == excluded
    assert a.get('/api/v1/cases/'+cid).json()['similar_cases']['count'] == 0
    assert a.get('/api/v1/cases').json()[0]['similar_cases']['excluded_count'] == 1
    with connect() as c:
        import json
        saved = c.execute('SELECT response FROM similar_cases WHERE case_id=?',(cid,)).fetchone()[0]
        assert json.loads(saved) == original['response']
    assert len(search) == 1


def test_exclusion_survives_analysis_and_can_be_restored(clients, search):
    a, _ = clients
    cid, _ = create(a)
    original = analyze(a, cid)
    excluded = relevance(a, cid, original['version']).json()
    rerun = analyze(a, cid, excluded['options'], excluded['version'])
    assert rerun['count'] == 0 and rerun['excluded_count'] == 1
    restored = relevance(a, cid, rerun['version'], relevant=True).json()
    assert restored['count'] == 1 and restored['excluded_count'] == 0
    assert restored['response']['items'] == original['response']['items']
    assert a.get(endpoint(cid)).json() == restored
    assert relevance(a, cid, restored['version'], relevant=True).json()['version'] == restored['version']


def test_restore_after_different_analysis_does_not_invent_a_matching_result(clients, search, monkeypatch):
    a, _ = clients
    cid, _ = create(a)
    original = analyze(a, cid)
    excluded = relevance(a, cid, original['version']).json()
    monkeypatch.setattr(main,'service',lambda method,path,payload:{'items':[],'applied_filters':payload,'warnings':[],'retrieval_mode':'semantic'})
    rerun = analyze(a, cid, {'query':'新的检索描述'}, excluded['version'])
    assert not rerun['excluded_items'][0]['in_current_results']
    assert rerun['excluded_from_current_count'] == 0
    restored = relevance(a, cid, rerun['version'], relevant=True).json()
    assert restored['count'] == restored['excluded_count'] == 0
    assert restored['response']['applied_filters']['query'] == '新的检索描述'


def test_relevance_permissions_conflicts_and_case_scope(clients, search):
    a, b = clients
    cid, _ = create(a)
    other, _ = create(a)
    original = analyze(a, cid)
    other_state = analyze(a, other)
    assert relevance(b, cid, original['version']).status_code == 404
    assert a.put(endpoint(cid)+'/judgment-1/relevance',json={'version':original['version'],'relevant':False},headers={'X-CSRF-Token':''}).status_code == 403
    assert relevance(a, cid, original['version'], doc_id='unknown-judgment').status_code == 404
    excluded = relevance(a, cid, original['version']).json()
    assert relevance(a, cid, original['version'], relevant=True).status_code == 409
    assert a.get(endpoint(cid)).json() == excluded
    assert a.get(endpoint(other)).json() == other_state
    assert a.put(endpoint(cid),json={'options':{'query':'旧页面的修改'},'version':original['version']}).status_code == 409


def test_slow_analysis_cannot_overwrite_a_relevance_edit(clients, search, monkeypatch):
    a, _ = clients
    cid, _ = create(a)
    original = analyze(a, cid)
    service = main.service
    def exclude_during_analysis(*args, **kwargs):
        with connect(True) as c:
            set_relevance(c,cid,'judgment-1',False,'local_admin')
        return service(*args, **kwargs)
    monkeypatch.setattr(main,'service',exclude_during_analysis)
    response = a.post(endpoint(cid)+'/analyze',json={'options':{},'version':original['version']})
    assert response.status_code == 409
    current = a.get(endpoint(cid)).json()
    assert current['excluded_count'] == 1 and current['count'] == 0
    assert current['analyzed_at'] == original['analyzed_at']
