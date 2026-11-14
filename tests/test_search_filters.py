"""Facet contracts on independently prepared CSV/SQLite fixtures, without an LLM."""
import csv
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace
from typing import get_args

import httpx
import pytest
from pydantic import ValidationError
from backend.app import settings
from backend.app.contracts import FacetInput, FilterKey, SearchInput
from backend.app.search_filters import FIELDS, value_id
from backend.app.search_service import SearchEngine
from scripts.prepare_corpus import terms
from scripts.prepare_filters import build_filters, source_hash


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CORPUS", tmp_path)
    monkeypatch.setattr(settings, "internal_key", lambda: "test-key")
    columns = [f.label for f in FIELDS]
    base = {label: "" for label in columns}
    rows = []
    for i, (province, city, court, cause, date) in enumerate([
        ("广东省", "珠海市", "甲法院", "商品房买卖合同纠纷", "2025-06-01"),
        ("广东省", "深圳市", "乙法院", "房屋租赁合同纠纷", "2025-06-02"),
        ("浙江省", "杭州市", "丙法院", "房屋租赁合同纠纷", "2025-06-03"),
        ("广东省", "", "甲法院", "商品房买卖合同纠纷", "2025-06-04"),
    ], 1):
        rows.append({**base, "标题": f"第{i}号房屋争议", "案号": f"(2025)测试{i}号", "法院": court,
                     "文书类型": "判决书", "案件类型": "民事", "审理程序": "一审", "判决时间": date,
                     "发布时间": "2025-06-10", "受理时间": "" if i==4 else "2025-05-01",
                     "案例级别": "普通案例", "法院级别": "基层法院", "案由": cause, "省": province, "市": city,
                     "判决年份": "2025", "判决结果": "支持" if i==1 else "", "法条": "民法典第一条\n第二条",
                     "文书内容": "房屋合同"*60 + f"第{i}号 末尾检索标记" + (" x' OR 1=1 --" if i==4 else "")})
    path = tmp_path / "source.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer=csv.DictWriter(f, fieldnames=columns);writer.writeheader();writer.writerows(rows)
    stored=[f for f in FIELDS if f.stored]
    with sqlite3.connect(tmp_path/"judgments.sqlite3") as c:
        c.execute("CREATE TABLE documents(id TEXT PRIMARY KEY,source_row INTEGER UNIQUE,"+
                  ",".join(f.key+" TEXT" for f in stored)+",sha256 TEXT)")
        c.execute("CREATE TABLE passages(id TEXT PRIMARY KEY,point_id INTEGER,document_id TEXT,section TEXT,start INTEGER,end INTEGER)")
        for i,row in enumerate(rows,1):
            body=row['文书内容']
            c.execute("INSERT INTO documents VALUES ("+','.join('?' for _ in range(len(stored)+3))+")",
                      [f"D{i}",i,*(row[f.label] for f in stored),hashlib.sha256(body.encode()).hexdigest()])
            c.execute("INSERT INTO passages VALUES (?,?,?,?,?,?)",(f"P{i}",i,f"D{i}","全文",0,len(body)))
    (tmp_path/"manifest.json").write_text(json.dumps({"source_sha256":source_hash(path)}))
    (tmp_path/"release.json").write_text(json.dumps({"release_id":"fixture","scope":"test"}))
    build_filters(tmp_path,path)
    with sqlite3.connect(tmp_path/"lexical.sqlite3") as c:
        c.execute("CREATE VIRTUAL TABLE passages_fts USING fts5(passage_id UNINDEXED,doc_id UNINDEXED,tokens)")
        c.executemany("INSERT INTO passages_fts VALUES (?,?,?)", [(f"P{i}",f"D{i}",' '.join(terms(r['文书内容']))) for i,r in enumerate(rows,1)])
    engine=SearchEngine.__new__(SearchEngine)
    engine.release={"release_id":"fixture","scope":"test"}
    engine.lock=threading.Lock();engine.vector_name="test"
    return engine,rows,path


def test_catalog_has_every_source_column_and_no_hidden_cap(corpus):
    engine,rows,_=corpus
    result=engine.facets(FacetInput())
    assert {f['key'] for f in result['fields']}==set(get_args(FilterKey))=={f.key for f in FIELDS}
    assert result['matching_documents']==4
    for field in FIELDS:
        first=engine.facets(FacetInput(field=field.key,limit=1))
        total=first['total_options']; seen=set();counts=0;offset=0
        while True:
            page=engine.facets(FacetInput(field=field.key,limit=1,offset=offset))
            for option in page['options']:
                assert option['value_id'] not in seen
                seen.add(option['value_id']);counts+=option['count']
            if not page['has_more']:break
            offset=page['next_offset']
        assert len(seen)==total==len({value_id(r[field.label]) for r in rows})
        assert counts==4


def test_all_18_columns_can_be_combined_for_same_document(corpus):
    engine,rows,_=corpus
    selected={f.key:[value_id(rows[0][f.label])] for f in FIELDS}
    assert engine.facets(FacetInput(filters=selected))['matching_documents']==1
    result=engine.search(SearchInput(query=rows[0]['案号'],mode='case_number',filters=selected))
    assert [r['document_id'] for r in result['items']]==['D1']
    assert result['applied_filters']['filters']==selected


def test_linked_options_counts_or_within_and_across(corpus):
    engine,_,_=corpus
    filters={'province':[value_id('广东省')],'city':[value_id('珠海市'),value_id('深圳市')]}
    assert engine.facets(FacetInput(filters=filters))['matching_documents']==2
    cities=engine.facets(FacetInput(field='city',filters=filters))
    assert {o['label']:o['count'] for o in cities['options']}=={'珠海市':1,'深圳市':1,'未载明（空值）':1}
    assert not any(o['label']=='杭州市' for o in cities['options'])
    filters['cause']=[value_id('商品房买卖合同纠纷')]
    assert engine.facets(FacetInput(filters=filters))['matching_documents']==1


def test_empty_values_and_conflicts_do_not_widen(corpus):
    engine,_,_=corpus
    assert engine.facets(FacetInput(filters={'city':['missing']}))['matching_documents']==1
    assert engine.facets(FacetInput(field='city',text='未载明'))['options'][0]['value_id']=='missing'
    assert engine.facets(FacetInput(field='city',text='空值'))['total_options']==1
    filters={'province':[value_id('浙江省')],'city':[value_id('珠海市')]}
    summary=engine.facets(FacetInput(filters=filters))
    assert summary['matching_documents']==0
    assert summary['selected']['city'][0]['label']=='珠海市'
    result=engine.search(SearchInput(query='房屋合同',mode='hybrid',filters=filters))
    assert result['items']==[] and result['applied_filters']['filters']==filters


def test_option_search_checks_full_field_and_binds_sql(corpus):
    engine,_,_=corpus
    options=engine.facets(FacetInput(field='content',text='末尾检索标记'))
    assert options['total_options']==4
    assert all('末尾检索标记' not in o['label'] for o in options['options'])
    assert engine.facets(FacetInput(field='content',text="x' OR 1=1 --"))['total_options']==1
    assert engine.facets(FacetInput(field='court',text="' OR 1=1 --"))['total_options']==0


def test_dates_legacy_conditions_and_facet_values_intersect(corpus):
    engine,_,_=corpus
    args=dict(province='广东省',date_from='2025-06-02',date_to='2025-06-03')
    assert engine.facets(FacetInput(**args))['matching_documents']==1
    result=engine.search(SearchInput(query='房屋合同',mode='keyword',**args,filters={'city':[value_id('深圳市')]}))
    assert [r['document_id'] for r in result['items']]==['D2']


def test_vector_and_degraded_lexical_keep_new_filters(corpus,monkeypatch):
    engine,_,_=corpus
    request=SearchInput(query='房屋合同',filters={'city':[value_id('深圳市')]})
    def failed(*args,**kwargs):raise httpx.ConnectError('test outage')
    monkeypatch.setattr('backend.app.search_service.httpx.post',failed)
    result=engine.search(request)
    assert result['degraded'] and [i['document_id'] for i in result['items']]==['D2']
    observed=[]
    def vector_query(*args,**kwargs):
        observed.extend(kwargs['query_filter'].must[0].match.any)
        # Deliberately inject an out-of-filter hit to test the final guard too.
        return SimpleNamespace(points=[SimpleNamespace(payload={'passage_id':'P1'}),SimpleNamespace(payload={'passage_id':'P2'})])
    engine.client=SimpleNamespace(query_points=vector_query)
    monkeypatch.setattr('backend.app.search_service.httpx.post',lambda *a,**kw:httpx.Response(200,json={'data':[{'embedding':[1,0]}]},request=httpx.Request('POST','http://local.test')))
    result=engine.search(request)
    assert result['retrieval_mode']=='hybrid' and not result['degraded']
    assert observed==['P2'] and [i['document_id'] for i in result['items']]==['D2']


@pytest.mark.parametrize('payload',[
    {'filters':{'unknown':['missing']}}, {'field':'bad SQL'}, {'filters':{'province':['raw value']}},
    {'filters':{'city':['missing']*51}}, {'date_from':'2025-06-03','date_to':'2025-06-01'},
    {'offset':-1}, {'limit':101},
])
def test_invalid_filters_rejected_at_contract(payload):
    with pytest.raises(ValidationError):FacetInput(**payload)


def test_changed_source_is_rejected_before_publish(corpus):
    _,_,path=corpus
    before=source_hash(settings.CORPUS/'filters.sqlite3')
    path.write_text(path.read_text(encoding='utf-8-sig')+'\n',encoding='utf-8-sig')
    with pytest.raises(RuntimeError,match='Source CSV hash differs'):build_filters(settings.CORPUS,path)
    assert source_hash(settings.CORPUS/'filters.sqlite3')==before


def test_semantic_mode_uses_vectors_only_and_keeps_filters(corpus,monkeypatch):
    engine,_,_=corpus
    # Pure semantic retrieval must work without the lexical index at all.
    (settings.CORPUS/'lexical.sqlite3').unlink()
    observed=[]
    def vector_query(*args,**kwargs):
        observed.extend(kwargs['query_filter'].must[0].match.any)
        return SimpleNamespace(points=[SimpleNamespace(payload={'passage_id':'P1'}),SimpleNamespace(payload={'passage_id':'P2'})])
    engine.client=SimpleNamespace(query_points=vector_query)
    monkeypatch.setattr('backend.app.search_service.httpx.post',lambda *a,**kw:httpx.Response(200,json={'data':[{'embedding':[1,0]}]},request=httpx.Request('POST','http://local.test')))
    result=engine.search(SearchInput(query='语义相似情形',mode='semantic',filters={'city':[value_id('深圳市')]}))
    assert result['retrieval_mode']=='semantic' and not result['degraded']
    assert observed==['P2'] and [i['document_id'] for i in result['items']]==['D2']


def test_semantic_failure_does_not_silently_become_keyword_search(corpus,monkeypatch):
    from fastapi import HTTPException
    engine,_,_=corpus
    def fail(*a,**kw):raise httpx.ConnectError('embedding unavailable')
    monkeypatch.setattr('backend.app.search_service.httpx.post',fail)
    with pytest.raises(HTTPException) as error:
        engine.search(SearchInput(query='房屋合同',mode='semantic'))
    assert error.value.status_code==503
