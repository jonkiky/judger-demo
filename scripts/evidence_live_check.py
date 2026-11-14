"""Observe and verify real local evidence runs; never inject model responses."""
import argparse
from datetime import datetime, timedelta
import json
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.evaluation_client import client,call,save,ROOT
from backend.app.db import digest


def main():
    p=argparse.ArgumentParser();p.add_argument('--record',default='evidence-live-run.json');p.add_argument('--report',default='evidence-live-results.json');args=p.parse_args()
    entry=json.loads((ROOT/'evaluation'/args.record).read_text());rid=entry['run_id'];c=client();last=None
    started=time.monotonic()
    while time.monotonic()-started<1900:
        run=call(c,'GET','/runs/'+rid)
        progress=call(c,'GET','/runs/'+rid+'/evidence')
        batch=progress['batch'];marker=(run['status'],run['stage'],batch['completed'] if batch else 0)
        if marker!=last:print(json.dumps({'run_id':rid,'status':marker[0],'stage':marker[1],'completed':marker[2]},ensure_ascii=False),flush=True);last=marker
        if run['status'] in ('succeeded','failed','cancelled','waiting_input'):break
        time.sleep(3)
    save(args.report,{'run_id':rid,'status':run['status'],'run':run,'progress':progress})
    if run['status']!='succeeded':raise RuntimeError('Real evidence run did not complete: '+str(run.get('error')))
    checks=[]
    assert batch and batch['completed']==batch['total']==5
    assert len(set(batch['selected_doc_ids']))==5
    assert all(agent['status']=='succeeded' for agent in batch['agents'])
    checks.append('five_distinct_real_cases_all_completed')
    if batch['selection_mode']=='automatic_top5':
        assert batch['selected_doc_ids']==[i['document_id'] for i in batch['candidates'][:5]]
        checks.append('default_uses_actual_top_five_without_user_selection')
    else:
        assert batch['selected_doc_ids']==entry['selected_doc_ids']
        assert batch['source_run_id']==entry['source_run_id']
        checks.append('reviewed_selection_created_new_run_with_exact_chosen_cases')
    assert not any(e['type']=='input.required' for e in run['events'])
    checks.append('no_case_selection_interrupt')
    calls=[m for m in run['model_calls'] if ':evidence_case:' in m['operation_key']]
    assert len(calls)==5 and all(m['status']=='succeeded' for m in calls)
    synthesis=[m for m in run['model_calls'] if ':evidence_synthesis:' in m['operation_key']]
    assert len(synthesis)==1 and synthesis[0]['status']=='succeeded'
    checks.append('five_separate_local_llm_case_requests_plus_one_synthesis')
    finished=[e for e in run['events'] if e['type']=='evidence.agent.completed']
    synth_event=next(e for e in run['events'] if e['type']=='evidence.synthesis.started')
    assert len(finished)==5 and max(e['seq'] for e in finished)<synth_event['seq']
    assert synth_event['payload']['responses_received']==5
    checks.append('synthesis_started_only_after_all_five_responses')
    for agent in batch['agents']:
        doc=call(c,'GET','/judgments/'+agent['doc_id'])
        assert digest(doc['content'])==doc['sha256']
        assert {s['doc_id'] for s in agent['sources']}=={agent['doc_id']}
        for source in agent['sources']:
            assert doc['content'][source['start']:source['end']]==source['quote']
            assert source['text_sha256']==doc['sha256']
            assert source['release_id']==doc['release_id']
        for finding in agent['result']['findings']:
            if finding['treatment'] in ('采信','不采信'):
                quote=finding.get('treatment_quote','')
                assert quote and finding['evidence'] in quote
                assert any(quote in s['quote'] for s in agent['sources'] if s['id'] in finding['source_ids'])
        assert all(question.endswith(('?','？')) for question in agent['result']['limits'])
    checks.append('all_agent_sources_trace_to_one_real_judgment_each')
    checks.append('admission_labels_have_literal_support_and_followups_are_questions')
    artifacts=[call(c,'GET','/artifacts/'+aid) for aid in run['artifacts']]
    artifact=next(a for a in artifacts if a['kind']=='evidence')
    used={fid for item in artifact['structured']['items'] for fid in item['finding_ids']}
    assert all(any(f['id'] in used for f in agent['result']['findings']) for agent in batch['agents'])
    checks.append('final_synthesis_includes_every_selected_case')
    # Requests overlap at the application layer; no claim about GPU parallelism.
    events=[]
    for request in calls:
        start=datetime.fromisoformat(request['created_at'])
        events.extend([(start,1),(start+timedelta(milliseconds=request['elapsed_ms']),-1)])
    current=peak=0
    for _,delta in sorted(events):current+=delta;peak=max(peak,current)
    assert peak>=2
    checks.append('independent_case_request_intervals_overlap')
    report={'run_id':rid,'case_id':entry['case_id'],'status':run['status'],'checks':checks,
            'selection_mode':batch['selection_mode'],'selected_doc_ids':batch['selected_doc_ids'],
            'artifact_id':artifact['id'],'case_model_calls':len(calls),'synthesis_model_calls':len(synthesis),
            'max_overlapping_agents':peak,'retrieval':batch['retrieval'],'run':run,'progress':progress}
    save(args.report,report)
    output=ROOT/'evaluation'/('evidence-multi-agent-reviewed.md' if batch['selection_mode']=='reviewed' else 'evidence-multi-agent-auto.md')
    output.write_text(artifact['body'])
    (ROOT/'docs/openapi.json').write_text(json.dumps(c.get('http://127.0.0.1:8765/openapi.json').json(),ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ('run','progress')},ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
