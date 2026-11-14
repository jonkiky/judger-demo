"""Versioned timeline edits against FastAPI and isolated SQLite storage."""
import json
import pytest

from backend.app.db import connect, digest, dumps, now
from backend.app.workflows import bounded_facts, model_sources, timeline
from backend.app.materials import store_material, validate_material_ref
from test_contracts import clients, create, run, seed_artifact


@pytest.fixture
def timeline_case(clients):
    owner, other = clients
    cid, mid = create(owner)
    rid = run(owner, cid).json()["id"]
    seed_artifact(cid, rid, mid)
    original = owner.get('/api/v1/artifacts/test_art').json()
    quote = original['sources'][0]['quote']
    facts = [{'id': 'M1', 'date_text': '2026年5月31日', 'kind': '约定时间',
              'quote': quote, 'source_ref': 'M1', 'status': '材料记载 · 待律师核实'}]
    body = '# 案情与时间线\n\n' + quote + ' [M1]'
    with connect(True) as db:
        db.execute("UPDATE artifacts SET kind='timeline',title='案情与时间线',body=?,content_hash=?,structured=? WHERE id='test_art'",
                   (body, digest(body), dumps({'facts': facts, 'conflicts': '日期精度须核对。'})))
    original = owner.get('/api/v1/artifacts/test_art').json()
    payload = {'content_hash': original['content_hash'], 'facts': [
        {'id': 'M1', 'date_text': '2026年5月', 'kind': '交房约定',
         'description': '合同约定的交房时间，具体日期待核对。', 'source_ref': 'M1'}]}
    return owner, other, cid, rid, mid, original, payload


def test_timeline_edit_persists_fields_without_rewriting_sources(timeline_case):
    owner, _, cid, _, _, original, payload = timeline_case
    assert owner.post('/api/v1/artifacts/test_art/reviews', json={'content_hash': original['content_hash'], 'confirmed': True}).status_code == 200
    result = owner.post('/api/v1/artifacts/test_art/versions', json=payload)
    assert result.status_code == 201, result.text
    saved = result.json()
    fact = saved['structured']['facts'][0]
    assert all(fact[key] == value for key, value in payload['facts'][0].items())
    assert fact['quote'] == original['sources'][0]['quote']
    assert fact['edited'] and '人工整理' in fact['status']
    assert saved['sources'] == original['sources']
    assert saved['version'] == 2 and saved['parent_id'] == original['id']
    assert saved['review_status'] == 'pending' and saved['reviewed_at'] is None
    assert saved['content_hash'] == digest(saved['body'])
    assert fact['description'] in saved['body'] and fact['quote'] in saved['body']
    assert saved['checks']['passed']
    reloaded = owner.get('/api/v1/cases/' + cid).json()['artifacts'][0]
    assert reloaded['id'] == saved['id'] and reloaded['structured'] == saved['structured']
    assert owner.get('/api/v1/artifacts/test_art').json()['structured'] == original['structured']
    assert owner.get('/api/v1/artifacts/' + saved['id'] + '/export').status_code == 409


def test_add_remove_reorder_events_and_reuse_source_ids(timeline_case):
    owner, _, _, _, _, original, payload = timeline_case
    new_fact = {**payload['facts'][0], 'id': 'fact_new', 'description': '补充一个需核实的履约节点。'}
    payload['facts'].insert(0, new_fact)
    saved = owner.post('/api/v1/artifacts/test_art/versions', json=payload).json()
    assert [fact['id'] for fact in saved['structured']['facts']] == ['fact_new', 'M1']
    bounded = bounded_facts({'facts': saved['structured']['facts']})
    assert [fact['id'] for fact in bounded] == ['M1', 'M1']
    assert bounded[0]['quote'] == original['sources'][0]['quote']
    assert bounded[0]['description'] == new_fact['description']
    assert bounded_facts({'facts': saved['structured']['facts']}, budget=1) == []
    result = owner.post('/api/v1/artifacts/' + saved['id'] + '/versions', json={'content_hash': saved['content_hash'], 'facts': [new_fact]})
    assert result.status_code == 201
    assert [fact['id'] for fact in result.json()['structured']['facts']] == ['fact_new']


def test_timeline_concurrency_and_case_access(timeline_case):
    owner, other, _, _, _, _, payload = timeline_case
    assert other.post('/api/v1/artifacts/test_art/versions', json=payload).status_code == 404
    assert owner.post('/api/v1/artifacts/test_art/versions', json={**payload, 'content_hash': 'old'}).status_code == 409
    assert owner.post('/api/v1/artifacts/test_art/versions', json=payload).status_code == 201
    assert owner.post('/api/v1/artifacts/test_art/versions', json=payload).status_code == 409


@pytest.mark.parametrize('change', [
    {'description': '   '}, {'kind': '  '}, {'source_ref': 'M999'},
    {'quote': '不能伪造原文'}, {'description': 'a' * 5001}, {'date_text': 'a' * 121},
])
def test_invalid_timeline_edits_do_not_create_versions(timeline_case, change):
    owner, _, cid, _, _, _, payload = timeline_case
    payload['facts'][0].update(change)
    assert owner.post('/api/v1/artifacts/test_art/versions', json=payload).status_code == 422
    assert len(owner.get('/api/v1/cases/' + cid).json()['artifacts']) == 1


def test_empty_duplicate_and_body_only_edits_are_rejected(timeline_case):
    owner, _, _, _, _, original, payload = timeline_case
    assert owner.post('/api/v1/artifacts/test_art/versions', json={**payload, 'facts': []}).status_code == 422
    assert owner.post('/api/v1/artifacts/test_art/versions', json={**payload, 'facts': payload['facts'] * 2}).status_code == 422
    assert owner.post('/api/v1/artifacts/test_art/versions', json={'body': 'New body [M1]', 'content_hash': original['content_hash']}).status_code == 422
    with connect(True) as db:
        db.execute("UPDATE artifacts SET kind='document' WHERE id='test_art'")
    assert owner.post('/api/v1/artifacts/test_art/versions', json=payload).status_code == 422


def test_new_generation_blocks_editing_old_root(timeline_case):
    owner, _, _, _, _, _, payload = timeline_case
    with connect(True) as db:
        original = dict(db.execute("SELECT * FROM artifacts WHERE id='test_art'").fetchone())
        original.update(id='art_new_generation', created_at=now(), operation_key='new-generation')
        db.execute(f"INSERT INTO artifacts ({','.join(original)}) VALUES ({','.join('?' for _ in original)})", tuple(original.values()))
    response = owner.post('/api/v1/artifacts/test_art/versions', json=payload)
    assert response.status_code == 409 and '新版本' in response.json()['detail']


def test_withdrawn_source_cannot_be_edited(timeline_case):
    owner, _, _, _, mid, _, payload = timeline_case
    assert owner.delete('/api/v1/materials/' + mid).status_code == 200
    assert owner.post('/api/v1/artifacts/test_art/versions', json=payload).status_code == 422


def test_workflow_reuses_saved_timeline(timeline_case, monkeypatch):
    owner, _, _, rid, _, _, payload = timeline_case
    saved = owner.post('/api/v1/artifacts/test_art/versions', json=payload).json()
    with connect() as db:
        snapshot = json.loads(db.execute('SELECT snapshot FROM runs WHERE id=?', (rid,)).fetchone()[0])
    monkeypatch.setattr('backend.app.workflows.guard', lambda *args: None)
    result = timeline({'run_id': rid, 'snapshot': snapshot})
    assert result['facts'] == saved['structured']['facts']
    assert result['outputs'] == [saved['id']]


def test_link_new_uploaded_material_and_custom_type(timeline_case):
    owner, _, cid, _, _, original, payload = timeline_case
    uploaded = owner.post('/api/v1/cases/' + cid + '/materials',
                          files={'file': ('现场记录.txt', '现场复核材料，供核对。'.encode(), 'text/plain')})
    assert uploaded.status_code == 201
    mid = uploaded.json()['id']
    fact = payload['facts'][0]
    fact.pop('source_ref')
    fact.update(material_id=mid, kind='  现场查勘  ')
    response = owner.post('/api/v1/artifacts/test_art/versions', json=payload)
    assert response.status_code == 201, response.text
    saved = response.json()
    event = saved['structured']['facts'][0]
    ref = next(item for item in saved['sources'] if item['id'] == event['source_ref'])
    assert event['material_id'] == mid and event['kind'] == '现场查勘'
    assert ref['doc_id'] == mid and ref['name'] == '现场记录.txt'
    assert ref['scope'] == 'document' and ref['quote'] == event['quote'] == ''
    assert '关联材料：现场记录.txt' in saved['body'] and saved['checks']['passed']
    assert saved['stale']  # Linking a new file does not pretend the original analysis was regenerated.
    assert owner.get('/api/v1/materials/' + ref['doc_id']).json()['text'].strip() == '现场复核材料，供核对。'
    reloaded = owner.get('/api/v1/cases/' + cid).json()['artifacts'][0]
    assert reloaded['structured']['facts'][0] == event
    assert owner.get('/api/v1/artifacts/test_art').json()['sources'] == original['sources']
    assert bounded_facts({'facts': [event]}) == []
    assert model_sources([ref]) == []
    with connect() as db:
        assert validate_material_ref(db, cid, ref)
        assert not validate_material_ref(db, cid, {**ref, 'quote': '伪造原文'})
        assert not validate_material_ref(db, cid, {**ref, 'file_sha256': 'incorrect'})


def test_file_selection_preserves_existing_quote_and_deduplicates_links(timeline_case):
    owner, _, _, _, mid, original, payload = timeline_case
    payload['facts'][0]['material_id'] = mid
    payload['facts'].append({'id': 'fact_added', 'date_text': '', 'kind': '现场查勘',
                             'description': '补充核对。', 'material_id': mid})
    saved = owner.post('/api/v1/artifacts/test_art/versions', json=payload).json()
    existing, added = saved['structured']['facts']
    assert existing['source_ref'] == 'M1' and existing['quote'] == original['sources'][0]['quote']
    assert added['material_id'] == mid and added['quote'] == ''
    payload = {'content_hash': saved['content_hash'], 'facts': [
        {**payload['facts'][0], 'kind': '协商'},
        {**payload['facts'][1], 'source_ref': added['source_ref']},
        {**payload['facts'][1], 'id': 'fact_another'},
    ]}
    result = owner.post('/api/v1/artifacts/' + saved['id'] + '/versions', json=payload)
    assert result.status_code == 201, result.text
    assert len(result.json()['sources']) == len(saved['sources'])


@pytest.mark.parametrize('invalid_kind', ['foreign', 'withdrawn', 'replaced', 'mismatch'])
def test_material_selection_enforces_current_case_and_version(timeline_case, invalid_kind):
    owner, other, cid, _, _, _, payload = timeline_case
    if invalid_kind == 'foreign':
        _, mid = create(other)
    else:
        mid = owner.post('/api/v1/cases/' + cid + '/materials/text',
                          json={'name': '补充材料', 'text': '补充材料正文。'}).json()['id']
        if invalid_kind == 'withdrawn':
            owner.delete('/api/v1/materials/' + mid)
        if invalid_kind == 'replaced':
            owner.post('/api/v1/materials/' + mid + '/versions', json={'name': '修订材料', 'text': '修订正文。'})
    if invalid_kind != 'mismatch':
        payload['facts'][0].pop('source_ref')
    payload['facts'][0]['material_id'] = mid
    response = owner.post('/api/v1/artifacts/test_art/versions', json=payload)
    assert response.status_code == 422


def test_uploaded_file_without_extracted_text_can_be_linked(timeline_case):
    owner, _, cid, _, _, _, payload = timeline_case
    with connect(True) as db:
        mid = store_material(db, cid, '扫描件.pdf', '', '当事人提供材料',
                             raw=b'isolated scan fixture', status='needs_transcription')
    payload['facts'][0].pop('source_ref')
    payload['facts'][0]['material_id'] = mid
    result = owner.post('/api/v1/artifacts/test_art/versions', json=payload)
    assert result.status_code == 201, result.text
    ref = next(item for item in result.json()['sources'] if item['doc_id'] == mid)
    with connect() as db:
        assert validate_material_ref(db, cid, ref)
    assert owner.get('/api/v1/materials/' + mid + '/download').status_code == 200
