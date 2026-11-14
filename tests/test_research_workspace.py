"""Research orchestration and document lifecycle against isolated SQLite/API.

Model responses are controlled fixtures; live-model verification is separate.
"""
from copy import deepcopy
import io
import json
import threading
import time
import zipfile
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from backend.app import workflows as w, research_agents as ra, research_workspace as rw, main
from backend.app.db import connect, digest, dumps
from backend.app.materials import material_ref
from test_contracts import clients, create, run


@pytest.fixture
def research(clients, monkeypatch):
    a, b = clients
    cid, mid = create(a)
    rid = run(a, cid, workflow="case.assess", goal="研究本案诉讼路径").json()["id"]
    snapshot = a.get('/api/v1/runs/'+rid).json()['snapshot']
    with connect(True) as c:
        c.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
        m = dict(c.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone())
    ref = {**material_ref(m, m['text']), 'id': 'M1'}
    state = {'run_id': rid, 'snapshot': snapshot, 'plan': ['case.timeline', 'case.assess'], 'facts': [{'id': 'M1', 'quote': ref['quote']}], 'sources': [ref], 'outputs': [], 'started': time.time()}
    documents, candidates = {}, []
    for i in range(1, 8):
        body = f'第{i}案。法院查明双方约定交房期限。本院认为应当依约履行。判决如下：履行交付义务。'
        doc = {'id': f'J{i}', 'title': f'测试类案{i}', 'case_number': f'(2025)测试{i}号', 'court': '测试法院', 'judgment_date': '2025-06-01', 'content': body, 'sha256': digest(body), 'release_id': 'fixture'}
        documents[doc['id']] = doc
        candidates.append({'document_id': doc['id'], **{k: doc[k] for k in ('title', 'case_number', 'court', 'judgment_date')}})
    def service(method, path, *args, **kwargs):
        return deepcopy(documents[path.rsplit('/', 1)[-1]])
    monkeypatch.setattr(ra, 'service', service)
    monkeypatch.setattr(w, 'service', service)
    monkeypatch.setattr(main, 'service', service)
    monkeypatch.setattr(w, 'retrieve_cases', lambda *args: {'candidates': deepcopy(candidates), 'retrieval': []})
    calls, lock = [], threading.Lock()
    def generate(op, system, payload, *args, **kwargs):
        with lock:
            calls.append((op, deepcopy(payload)))
        if ':research_case:' in op:
            assert set(payload) == {'judgment', 'coverage', 'sources'}
            assert {s['case_number'] for s in payload['sources']} == {payload['judgment']['case_number']}
            return {'findings': [{'issue': '合同履行', 'facts': '判决记载约定交房期限。', 'reasoning': '本院认为应当依约履行。', 'outcome': '履行交付义务。', 'source_ids': [payload['sources'][0]['id']]}], 'limits': ['需结合案件差异核对。']}
        if ':research_synthesis:' in op:
            with connect() as c:
                assert all(r[0] == 'succeeded' for r in c.execute('SELECT status FROM research_agents WHERE run_id=?', (rid,)))
            return {'sections': [{'heading': '逐案比较', 'paragraphs': [{'text': '该案记载交付义务，本案应核对履行情况。', 'source_ids': r['findings'][0]['source_ids']} for r in payload['case_reports']]}, {'heading': '本案材料', 'paragraphs': [{'text': '合同约定交房日期，需核对原件。', 'source_ids': ['M1']}]}], 'unresolved': ['核对通知到达情况。']}
        assert ':research_followup' in op
        return {'answer': '已细化下一步核对材料与行动顺序。', 'sections': [{'heading': '完善后的行动顺序', 'paragraphs': [{'text': '先核对合同原件，再核实交付与通知情况。', 'source_ids': ['M1']}]}], 'unresolved': ['尚需核对原件。']}
    monkeypatch.setattr(ra, 'generate', generate)
    monkeypatch.setattr(rw, 'generate', generate)
    return dict(a=a, b=b, cid=cid, mid=mid, rid=rid, state=state, candidates=candidates, documents=documents, calls=calls, generate=generate)


def finish(x):
    result = ra.run_research(x['state'])
    with connect(True) as c:
        c.execute("UPDATE runs SET status='succeeded' WHERE id=?", (x['rid'],))
    return x['a'].get('/api/v1/artifacts/'+result['outputs'][-1]).json()


def test_default_three_independent_agents_join_and_idempotent_recovery(research):
    x = research
    a = finish(x)
    assert a['structured']['multi_agent']['total'] == 3
    assert len(x['calls']) == 4
    progress = x['a'].get('/api/v1/runs/'+x['rid']+'/research').json()['batch']
    assert progress['completed'] == 3 and progress['status'] == 'succeeded'
    assert progress['selection_mode'] == 'automatic_default'
    ra.run_research(x['state'])
    assert len(x['calls']) == 4
    assert x['b'].get('/api/v1/runs/'+x['rid']+'/research').status_code == 404
    with connect() as c:
        types = [r[0] for r in c.execute('SELECT type FROM events WHERE run_id=? ORDER BY seq', (x['rid'],))]
    assert max(i for i, t in enumerate(types) if t == 'research.agent.completed') < types.index('research.synthesis.started')


def test_failed_agent_blocks_synthesis_and_successful_agents_survive(research, monkeypatch):
    x = research
    def failing(op, *args, **kwargs):
        if ':research_case:J3:1' in op:
            raise RuntimeError('provider unavailable')
        return x['generate'](op, *args, **kwargs)
    monkeypatch.setattr(ra, 'generate', failing)
    with pytest.raises(ValueError, match='2/3'):
        ra.run_research(x['state'])
    assert not any(':research_synthesis:' in op for op, _ in x['calls'])
    finish(x)
    with connect() as c:
        attempts = dict(c.execute('SELECT doc_id,attempt FROM research_agents WHERE run_id=?', (x['rid'],)))
    assert attempts == {'J1': 1, 'J2': 1, 'J3': 2}


def test_missing_case_rejects_summary_and_retries_only_coordinator(research, monkeypatch):
    x = research
    def missing(op, *args, **kwargs):
        result = x['generate'](op, *args, **kwargs)
        if ':research_synthesis:1' in op:
            result['sections'][0]['paragraphs'].pop()
        return result
    monkeypatch.setattr(ra, 'generate', missing)
    with pytest.raises(ValueError, match='遗漏'):
        ra.run_research(x['state'])
    finish(x)
    assert len([op for op, _ in x['calls'] if ':research_case:' in op]) == 3


def test_missing_case_gets_a_grounded_comparison_before_delivery(research, monkeypatch):
    x = research
    repaired = []
    def complete(op, system, payload, *args, **kwargs):
        if op.endswith(':coverage_repair'):
            repaired.extend(r['doc_id'] for r in payload['case_reports'])
            return {r['doc_id']: {'text': '该案涉及依约交付，本案的交付及通知事实仍需核实。', 'source_ids': r['findings'][0]['source_ids']} for r in payload['case_reports']}
        result = x['generate'](op, system, payload, *args, **kwargs)
        if ':research_synthesis:' in op:
            result['sections'][0]['paragraphs'].pop()
        return result
    monkeypatch.setattr(ra, 'generate', complete)
    artifact = finish(x)
    assert repaired == ['J3']
    assert artifact['structured']['sections'][-1]['heading'] == '补充类案对照'
    assert artifact['structured']['multi_agent']['completed'] == 3


@pytest.mark.parametrize('ids', [['J1']*2, ['J'+str(i) for i in range(1, 7)]])
def test_selection_limit_enforced_at_api_and_runner(research, ids):
    x = research
    assert x['a'].put('/api/v1/cases/'+x['cid']+'/research-selections', json={'doc_ids': ids}).status_code == 422
    x['state']['selected'] = ids
    with pytest.raises(ValueError):
        ra.prepare_batch(x['state'])


def test_duplicate_case_numbers_rejected_and_auto_deduplicated(research):
    x = research
    x['documents']['J2']['case_number'] = x['documents']['J1']['case_number']
    assert x['a'].put('/api/v1/cases/'+x['cid']+'/research-selections', json={'doc_ids': ['J1', 'J2']}).status_code == 422
    x['candidates'][1]['case_number'] = x['candidates'][0]['case_number']
    assert ra.prepare_batch(x['state'])['selected_doc_ids'] == ['J1', 'J3', 'J4']


def test_saved_filtered_results_use_default_three_without_new_search(research, monkeypatch):
    x = research
    x['state']['snapshot']['research_candidates'] = x['candidates'][3:]
    monkeypatch.setattr(w, 'retrieve_cases', lambda *args: pytest.fail('saved filters must be retained'))
    batch = ra.prepare_batch(x['state'])
    assert batch['selected_doc_ids'] == ['J4', 'J5', 'J6']
    assert batch['selection_mode'] == 'filtered_default' and len(batch['candidates']) == 4


@pytest.mark.parametrize('count', [1, 2])
def test_short_candidate_list_uses_available_cases(research, count):
    x = research
    x['candidates'][:] = x['candidates'][:count]
    assert len(ra.prepare_batch(x['state'])['selected_doc_ids']) == count


def test_document_selection_prompt_defaults_to_three_distinct_cases(research, monkeypatch):
    x = research
    candidates = deepcopy(x['candidates'])
    candidates[1]['case_number'] = candidates[0]['case_number']
    prompts = []
    def answer(prompt):
        prompts.append(prompt)
        return {'doc_ids': prompt['default_doc_ids']}
    monkeypatch.setattr(w, 'interrupt', answer)
    state = {**x['state'], 'plan': ['document.generate'], 'candidates': candidates,
             'snapshot': {**x['state']['snapshot'], 'document_type': 'complaint'}}
    assert w.select_cases(state)['selected'] == ['J1', 'J3', 'J4']
    assert len(prompts[0]['candidates']) == 7
    monkeypatch.setattr(w, 'interrupt', lambda prompt: {'doc_ids': []})
    assert w.select_cases(state)['selected'] == []


def test_no_cases_is_explicit_failure(research):
    x = research
    x['candidates'].clear()
    with pytest.raises(ValueError):
        ra.run_research(x['state'])
    assert not x['calls']


@pytest.mark.parametrize('ids', [['J7'], ['J2', 'J3', 'J4', 'J5', 'J6']])
def test_user_selected_scope_overrides_default_three(research, ids):
    x = research
    x['state']['selected'] = ids
    artifact = finish(x)
    assert artifact['structured']['multi_agent']['selected_doc_ids'] == ids
    assert len(x['calls']) == len(ids)+1


def test_mismatched_law_citation_is_repaired_and_rechecked(research, monkeypatch):
    x = research
    sources = [{'id': 'L563', 'source_kind': 'law', 'name': '民法典', 'quote': '第五百六十三条 当事人依法解除合同。'},
               {'id': 'L188', 'source_kind': 'law', 'name': '民法典', 'quote': '第一百八十八条 诉讼时效。'}]
    report = {'sections': [{'heading': '条件', 'paragraphs': [{'text': '应核实民法典第五百六十三条的解除条件。', 'source_ids': ['L188']}]}]}
    calls = []
    def repair(op, system, payload, *args, **kwargs):
        calls.append(op)
        assert {s['id'] for s in payload['sources']} == {'L563', 'L188'}
        return {'text': payload['paragraph']['text'], 'source_ids': ['L563']}
    monkeypatch.setattr(ra, 'generate', repair)
    ra.validate_report(x['state'], report, sources, 'repair-test')
    assert report['sections'][0]['paragraphs'][0]['source_ids'] == ['L563']
    assert len(calls) == 1
    report['sections'][0]['paragraphs'][0]['source_ids'] = ['L188']
    monkeypatch.setattr(ra, 'generate', lambda *args, **kwargs: {'text': '民法典第九百九十九条。', 'source_ids': ['L563']})
    with pytest.raises(ValueError, match='法条条号'):
        ra.validate_report(x['state'], report, sources, 'bad-repair-test')


def test_law_repair_keeps_named_judgment_and_material_sources(monkeypatch):
    # Regression: the original paragraph cited only a law. Law repair used to
    # discard the selected judgment and then falsely reject its case number.
    sources = [
        {'id': 'L_civil_563', 'source_kind': 'law', 'name': '民法典', 'quote': '第五百六十三条 解除条件。'},
        {'id': 'M1', 'source_kind': 'case_material', 'quote': '本案交付情况待核实。'},
        {'id': 'J1_1', 'source_kind': 'judgment', 'case_number': '(2025)浙0212民初2321号',
         'court': '测试法院', 'section': '裁判理由', 'judgment_date': '2025-06-01', 'quote': '应结合履约事实判断。'},
    ]
    report = {'sections': [{'heading': '对照', 'paragraphs': [{
        'text': '根据民法典第五百六十四条，结合类案（2025）浙0212民初2321号核查本案履约情况。',
        'source_ids': ['L_civil_563']}]}]}
    calls = []
    def repair(op, system, payload, *args, **kwargs):
        calls.append(op)
        assert {s['id'] for s in payload['sources']} == {'L_civil_563', 'M1', 'J1_1'}
        assert set(kwargs['output_schema']['properties']['source_ids']['items']['enum']) == {'L_civil_563', 'M1', 'J1_1'}
        return {'text': '根据民法典第五百六十三条，结合类案（2025）浙0212民初2321号核查本案履约情况。',
                'source_ids': ['L_civil_563', 'M1', 'J1_1']}
    monkeypatch.setattr(ra, 'generate', repair)
    ra.validate_report({'run_id': 'regression'}, report, sources, 'original-synthesis')
    assert calls == ['original-synthesis:citation_repair:v2:0:0']
    w.validate_paragraphs(report, sources)


def test_case_number_normalization_and_cited_original_references():
    sources = [{'id': 'J1', 'case_number': '（２０２５） 浙０２ 民终３０６７号',
                'quote': '原审案号为（2024）浙0212民初321号。'},
               {'id': 'M1', 'quote': '本案案号为（2026）粤0402民初123号。'}]
    def check(text, ids):
        return w.validate_paragraphs({'sections': [{'heading': '对照', 'paragraphs': [{'text': text, 'source_ids': ids}]}]}, sources)
    check('（2025）浙02民终3067号', ['J1'])
    check('原审案号（2024）浙0212民初321号。', ['J1'])
    check('本案（2026）粤0402民初123号。', ['M1'])
    with pytest.raises(w.CaseNumberSourceError, match='2026.*粤0402民初123号'):
        check('本案（2026）粤0402民初123号。', ['J1'])
    with pytest.raises(w.CaseNumberSourceError, match='2025.*京0101民初999号'):
        check('未经提供的（2025）京0101民初999号。', ['J1', 'M1'])


def test_case_number_repair_cannot_introduce_an_unselected_judgment(monkeypatch):
    sources = [{'id': 'M1', 'source_kind': 'case_material', 'quote': '交付情况待核实。'}]
    report = {'sections': [{'heading': '对照', 'paragraphs': [{'text': '参考（2025）京0101民初999号。', 'source_ids': ['M1']}]}]}
    monkeypatch.setattr(ra, 'generate', lambda *args, **kwargs: {'text': '参考（2025）京0101民初888号。', 'source_ids': ['M1']})
    with pytest.raises(w.CaseNumberSourceError, match='2025.*京0101民初888号'):
        ra.validate_report({'run_id': 'regression'}, report, sources, 'bad-case-repair')


def test_repair_has_one_final_grounded_attempt_and_reuses_original_source_scope(monkeypatch):
    sources = [{'id': 'L_civil_563', 'source_kind': 'law', 'name': '民法典', 'quote': '第五百六十三条 解除条件。'},
               {'id': 'L_civil_565', 'source_kind': 'law', 'name': '民法典', 'quote': '第五百六十五条 通知。'}]
    report = {'sections': [{'heading': '条件', 'paragraphs': [{'text': '根据第五百六十三条核对解除条件。', 'source_ids': ['L_civil_565']}]}]}
    calls = []
    def repair(op, system, payload, *args, **kwargs):
        calls.append(op)
        assert {s['id'] for s in payload['sources']} == {'L_civil_563', 'L_civil_565'}
        if op.endswith(':retry'):
            return {'text': '需根据给定法律原文核对解除条件与通知情况。', 'source_ids': ['L_civil_563', 'L_civil_565']}
        return payload['paragraph']
    monkeypatch.setattr(ra, 'generate', repair)
    ra.validate_report({'run_id': 'regression'}, report, sources, 'final-repair')
    assert len(calls) == 2 and calls[-1].endswith(':retry')
    w.validate_paragraphs(report, sources)
    calls.clear()
    report['sections'][0]['paragraphs'][0] = {'text': '根据第五百六十三条。', 'source_ids': ['L_civil_565']}
    monkeypatch.setattr(ra, 'generate', lambda op, system, payload, *args, **kwargs: (calls.append(op) or payload['paragraph']))
    with pytest.raises(ValueError, match='法条条号'):
        ra.validate_report({'run_id': 'regression'}, report, sources, 'final-repair-still-invalid')
    assert len(calls) == 2


def test_graph_completes_without_selection_interrupt(research, monkeypatch):
    x = research
    monkeypatch.setattr(w, 'timeline', lambda state: {'facts': x['state']['facts'], 'sources': x['state']['sources'], 'outputs': []})
    graph = w.build_graph(InMemorySaver())
    config = {'configurable': {'thread_id': x['rid']}}
    result = graph.invoke(x['state'], config)
    assert result['outputs'] and not graph.get_state(config).tasks


def test_chat_refines_report_and_word_lifecycle(research):
    x = research
    artifact = finish(x)
    path = '/api/v1/cases/'+x['cid']+'/requests'
    req = {'workflow': 'research.followup', 'goal': '请细化下一步行动', 'research_artifact_id': artifact['id']}
    response = x['a'].post(path, json=req, headers={'Idempotency-Key': 'research-chat-test'})
    assert response.status_code == 202, response.text
    rid = response.json()['id']
    assert x['a'].post(path, json=req, headers={'Idempotency-Key': 'research-chat-busy'}).status_code == 409
    snapshot = x['a'].get('/api/v1/runs/'+rid).json()['snapshot']
    state = {'run_id': rid, 'snapshot': snapshot, 'plan': ['research.followup'], 'outputs': []}
    result = rw.followup(state)
    revised = x['a'].get('/api/v1/artifacts/'+result['outputs'][-1]).json()
    assert revised['version'] == 2 and revised['parent_id'] == artifact['id']
    assert revised['structured']['conversation'][0]['question'] == req['goal']
    assert '完善后的行动顺序' in revised['body']
    endpoint = '/api/v1/artifacts/'+revised['id']+'/reference-documents'
    body = {'content_hash': revised['content_hash'], 'timezone': 'America/New_York'}
    headers = {'Idempotency-Key': 'research-word-test'}
    saved = x['a'].post(endpoint, json=body, headers=headers)
    assert saved.status_code == 201, saved.text
    doc = saved.json()
    assert x['a'].post(endpoint, json=body, headers=headers).json()['id'] == doc['id']
    download = '/api/v1/reference-documents/'+doc['id']+'/download'
    file = x['a'].get(download)
    assert file.status_code == 200
    with zipfile.ZipFile(io.BytesIO(file.content)) as archive:
        xml = archive.read('word/document.xml').decode()
    assert '完善后的行动顺序' in xml and '请细化下一步行动' in xml and '来源索引' in xml
    preview = x['a'].post(endpoint, json={**body, 'save_to_materials': False}, headers={'Idempotency-Key': 'research-word-preview'})
    assert preview.status_code == 200 and preview.content[:2] == b'PK'
    assert len(x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents']) == 1
    delete = '/api/v1/reference-documents/'+doc['id']
    assert x['b'].delete(delete).status_code == 404
    assert x['a'].delete(delete, headers={'X-CSRF-Token': ''}).status_code == 403
    assert x['a'].delete(delete).status_code == 200
    assert x['a'].delete(delete).status_code == 200
    assert x['a'].get(download).status_code == 404
    assert x['a'].get('/api/v1/cases/'+x['cid']).json()['reference_documents'] == []
    assert x['a'].post(endpoint, json=body, headers=headers).status_code == 409
    assert x['a'].get('/api/v1/artifacts/'+revised['id']).json()['body'] == revised['body']


def test_foreign_stale_and_wrong_kind_chat_rejected(research):
    x = research
    artifact = finish(x)
    path = '/api/v1/cases/'+x['cid']+'/requests'
    req = {'workflow': 'research.followup', 'goal': '继续完善研究', 'research_artifact_id': artifact['id']}
    assert x['b'].post(path, json=req, headers={'Idempotency-Key': 'foreign-chat-test'}).status_code == 404
    x['a'].post('/api/v1/cases/'+x['cid']+'/materials/text', json={'name': '补充', 'text': '新的待核实陈述。'})
    assert x['a'].post(path, json=req, headers={'Idempotency-Key': 'stale-chat-test'}).status_code == 409


def test_long_judgments_do_not_crowd_out_client_facts_and_law():
    judgments = [{'id': f'J{i}', 'source_kind': 'judgment', 'doc_id': f'case{i%5}', 'name': '测试类案',
                  'case_number': '(2025)测试号', 'court': '测试法院', 'section': '裁判理由', 'judgment_date': '2025-06-01', 'quote': '判决原文。'*160} for i in range(20)]
    primary = [{'id': 'M1', 'source_kind': 'case_material', 'doc_id': 'material', 'name': '本案陈述', 'quote': '尚待核实交付与通知情况。'},
               {'id': 'L563', 'source_kind': 'law', 'doc_id': 'law', 'name': '民法典', 'quote': '第五百六十三条，解除的前提条件。'}]
    payload = {'current_report': '此前研究。'*500, 'question': '请完善行动顺序。'}
    shown = rw.bounded_sources([*judgments, *primary], payload)
    assert {'M1', 'L563'}.issubset({s['id'] for s in shown})
    assert {s['doc_id'] for s in shown if s['source_kind'] == 'judgment'} == {f'case{i}' for i in range(5)}
    assert len(shown) < len(judgments)+len(primary)
    assert len(dumps({**payload, 'sources': w.model_sources(shown)})) < 12500


@pytest.fixture
def research_scope(research):
    """A saved case list and four materials, independent of model output."""
    from backend.app.contracts import SimilarCaseInput
    from backend.app.similar_cases import save_similar
    x = research
    x['material_ids'] = [x['mid']]
    for i in range(3):
        material = x['a'].post('/api/v1/cases/'+x['cid']+'/materials/text',
            json={'name': f'补充材料{i+1}', 'text': f'第{i+1}份补充材料，记载待核实事项。'}).json()
        x['material_ids'].append(material['id'])
    with connect(True) as c:
        revision = c.execute('SELECT source_revision FROM cases WHERE id=?', (x['cid'],)).fetchone()[0]
        save_similar(c, x['cid'], SimilarCaseInput(), response={'items': x['candidates'], 'release_id': 'fixture'}, source_revision=revision)
    return x


def selected_research(x, **overrides):
    return run(x['a'], x['cid'], key='explicit-research-scope', goal='比较类案与本案材料', workflow='case.assess',
        **{'material_ids': x['material_ids'][:3], 'research_selection': {'similar_version': 1, 'doc_ids': ['J7', 'J2']}, **overrides})


def test_research_sources_are_exact_and_immutable(research_scope, monkeypatch):
    x = research_scope
    # Old global choices cannot replace the explicit per-run selection.
    with connect(True) as c:
        c.executemany('INSERT INTO selections VALUES (?,?)', [(x['cid'], f'J{i}') for i in range(1, 6)])
    response = selected_research(x)
    assert response.status_code == 202, response.text
    rid = response.json()['id']
    snap = x['a'].get('/api/v1/runs/'+rid).json()['snapshot']
    assert snap['material_ids'] == x['material_ids'][:3]
    assert set(snap['material_hashes']) == set(x['material_ids'][:3])
    assert snap['selected_doc_ids'] == ['J7', 'J2']
    assert snap['research_selection']['similar_version'] == 1
    assert x['a'].get('/api/v1/cases/'+x['cid']).json()['selected_doc_ids'] == [f'J{i}' for i in range(1, 6)]
    assert selected_research(x).json() == {'id': rid, 'reused': True}
    # The next source-list edit does not widen or replace an already queued run.
    with connect(True) as c:
        c.execute('UPDATE similar_cases SET response=? WHERE case_id=?', (dumps({'items': []}), x['cid']))
    monkeypatch.setattr(w, 'retrieve_cases', lambda *args: pytest.fail('explicit selection must not search again'))
    state = {**x['state'], 'run_id': rid, 'snapshot': snap}
    batch = ra.prepare_batch(state)
    assert batch['selected_doc_ids'] == ['J7', 'J2']
    assert batch['selection_mode'] == 'filtered_selected'
    assert [m['id'] for m in w.get_materials(state)] == x['material_ids'][:3]


@pytest.mark.parametrize('ids', [[], ['J1', 'J1'], ['J1', 'J2', 'J3', 'J4'], ['foreign-case']])
def test_new_research_rejects_invalid_case_scope(research_scope, ids):
    assert selected_research(research_scope, research_selection={'similar_version': 1, 'doc_ids': ids}).status_code == 422


def test_research_rejects_four_materials_and_unreadable_materials(research_scope):
    x = research_scope
    assert selected_research(x, material_ids=x['material_ids']).status_code == 422
    with connect(True) as c:
        c.execute("UPDATE materials SET status='partial' WHERE id=?", (x['mid'],))
    assert selected_research(x).status_code == 422


def test_research_respects_case_list_version_and_removals(research_scope):
    x = research_scope
    removed = x['a'].put('/api/v1/cases/'+x['cid']+'/similar-cases/J7/relevance', json={'version': 1, 'relevant': False})
    assert removed.status_code == 200
    assert selected_research(x).status_code == 409
    assert selected_research(x, research_selection={'similar_version': 2, 'doc_ids': ['J7']}).status_code == 422


@pytest.mark.parametrize('state', ['missing', 'stale', 'empty', 'release'])
def test_explicit_research_does_not_fall_back_to_unselected_sources(research_scope, state):
    x = research_scope
    with connect(True) as c:
        if state == 'missing':
            c.execute('DELETE FROM similar_cases WHERE case_id=?', (x['cid'],))
        elif state == 'stale':
            c.execute('UPDATE similar_cases SET source_revision=0 WHERE case_id=?', (x['cid'],))
        elif state == 'empty':
            c.execute('UPDATE similar_cases SET response=? WHERE case_id=?', (dumps({'items': []}), x['cid']))
        else:
            c.execute('UPDATE similar_cases SET response=? WHERE case_id=?', (dumps({'items': x['candidates'], 'release_id': 'old'}), x['cid']))
    assert selected_research(x).status_code == (422 if state == 'empty' else 409)


def test_research_selection_requires_research_workflow_and_authorized_case(research_scope):
    x = research_scope
    args = {'research_selection': {'similar_version': 1, 'doc_ids': ['J1']}, 'material_ids': [x['mid']]}
    assert run(x['a'], x['cid'], key='wrong-workflow', workflow='case.timeline', **args).status_code == 422
    assert run(x['b'], x['cid'], key='wrong-owner', workflow='case.assess', **args).status_code == 404
