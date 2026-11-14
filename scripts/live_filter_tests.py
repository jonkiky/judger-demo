"""Read-only acceptance checks against the running 50k corpus and real Qdrant."""
import json
import sqlite3
import sys
import time
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.evaluation_client import client,call,save,ROOT
from backend.app import settings
from backend.app.search_filters import FIELDS,value_id


def main():
    c=client(); checks=[]; timings=[]
    def facets(**payload):
        started=time.monotonic();result=call(c,'POST','/search/facets',payload)
        timings.append(round((time.monotonic()-started)*1000));return result
    with sqlite3.connect(settings.CORPUS/'filters.sqlite3') as db:
        db.row_factory=sqlite3.Row
        report=json.loads(db.execute('SELECT body FROM filter_manifest').fetchone()[0])
        stats={r['field']:dict(r) for r in db.execute('SELECT * FROM field_stats')}
        start=facets()
        assert start['matching_documents']==50000 and len(start['fields'])==18
        inventory=[]
        for field in FIELDS:
            first=facets(field=field.key,limit=2)
            assert first['total_options']==stats[field.key]['distinct_values']
            last=facets(field=field.key,offset=first['total_options']-1,limit=2)
            assert len(last['options'])==1 and not last['has_more']
            for option in first['options']+last['options']:
                count=db.execute('SELECT count(*) FROM document_filters WHERE field=? AND value_id=?',(field.key,option['value_id'])).fetchone()[0]
                assert count==option['count']
            inventory.append({'field':field.key,'label':field.label,'values':first['total_options'],'missing_documents':stats[field.key]['missing_documents'],'last_option_accessible':True})
        checks.append('all_18_fields_counts_and_last_page_verified_against_sqlite')
        # Use an existing real judgment; do not add or rewrite any corpus document.
        exact=call(c,'POST','/search',{'query':'(2025)粤1303民初9812号','mode':'case_number'})
        did=exact['items'][0]['document_id']
        filters={r['field']:[r['value_id']] for r in db.execute('SELECT field,value_id FROM document_filters WHERE document_id=?',(did,))}
        summary=facets(filters=filters)
        assert summary['matching_documents']==1
        result=call(c,'POST','/search',{'query':'商品房买卖 按揭贷款 合同解除','filters':filters})
        assert result['retrieval_mode']=='hybrid' and not result['degraded']
        assert [i['document_id'] for i in result['items']]==[did]
        assert result['applied_filters']['filters']==filters
        checks.append('real_qdrant_hybrid_with_all_18_conditions_returns_only_matching_document')
        # A legal-looking but contradictory combination must not relax itself.
        bad={**filters,'province':[value_id('浙江省')]}
        assert facets(filters=bad)['matching_documents']==0
        zero=call(c,'POST','/search',{'query':'房屋合同','filters':bad})
        assert zero['items']==[] and zero['applied_filters']['filters']==bad
        checks.append('conflicting_combination_remains_zero')
        cities=facets(field='city',filters={'province':[value_id('广东省')]},limit=100)
        assert not cities['has_more']
        expected=dict(db.execute("SELECT x.value_id,count(*) FROM document_filters x JOIN document_filters p ON p.document_id=x.document_id WHERE x.field='city' AND p.field='province' AND p.value_id=? GROUP BY x.value_id",(value_id('广东省'),)))
        assert {o['value_id']:o['count'] for o in cities['options']}==expected
        checks.append('province_city_linked_options_equal_sqlite_intersection')
        missing=facets(filters={'court_level':['missing']})
        assert missing['matching_documents']==stats['court_level']['missing_documents']
        checks.append('imported_column_empty_value_is_selectable')
        assert c.post('/search/facets',json={'field':'not-a-column'}).status_code==422
        assert c.post('/search',json={'query':'房屋合同','filters':{'wrong':['missing']}}).status_code==422
        assert c.post('/search/facets',json={},headers={'X-CSRF-Token':''}).status_code==403
        with httpx.Client(trust_env=False) as anonymous:
            assert anonymous.post('http://127.0.0.1:8765/api/v1/search/facets',json={}).status_code==401
            assert anonymous.post('http://127.0.0.1:8767/facets',json={}).status_code==401
        checks.append('authentication_csrf_and_field_whitelist_enforced')
        report.update(checks=checks,inventory=inventory,hybrid_document=did,
                      hybrid_elapsed_ms=result['elapsed_ms'],facet_calls=len(timings),
                      max_facet_ms=max(timings),median_facet_ms=sorted(timings)[len(timings)//2])
    save('filter-live-results.json',report)
    (ROOT/'docs/openapi.json').write_text(json.dumps(c.get('http://127.0.0.1:8765/openapi.json').json(),ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
