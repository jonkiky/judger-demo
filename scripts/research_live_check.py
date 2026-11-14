"""Real-model acceptance run in a separate fictional case; never modifies source case."""
import json
import sys
import httpx
from pathlib import Path
from evaluation_client import client, call, save, ROOT
sys.path.insert(0, str(ROOT))
from backend.app.settings import DEFAULT_CASE_ANALYSIS_COUNT

record_path = ROOT/'evaluation/research-live-run.json'
runtime_path = ROOT/'evaluation/research-acceptance-runtime.json'
runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else None
if runtime:
    c = httpx.Client(base_url=runtime['base']+'/api/v1', trust_env=False, timeout=180)
    auth = c.post('/auth/login', json=json.loads(Path(runtime['credentials_path']).read_text()))
    auth.raise_for_status()
    c.headers['X-CSRF-Token'] = auth.json()['csrf']
else:
    c = client()
try:
    if sys.argv[1:] == ['start']:
        source = {'response': {'items': [{'document_id': did} for did in json.loads(record_path.read_text())['doc_ids']]}} if runtime else c.get('/cases/case_425879ebba3aa759ac7e4a9b/similar-cases').json()
        ids, seen = [], set()
        for item in source['response']['items']:
            identity = item.get('case_number') or item['document_id']
            if identity not in seen:
                seen.add(identity)
                ids.append(item['document_id'])
            if len(ids) == DEFAULT_CASE_ANALYSIS_COUNT:
                break
        assert 1 <= len(ids) <= DEFAULT_CASE_ANALYSIS_COUNT
        case = call(c, 'POST', '/cases', {'title': '研究功能验收 临时虚构案例', 'position': '购房人代理律师', 'claims': '核实逾期交房的履行与解除条件、证据及备位路径。'})
        call(c, 'POST', '/cases/'+case['id']+'/materials/text', {'name': '虚构验收材料', 'kind': '当事人陈述', 'text': '本材料仅为软件功能验收的虚构案情。买方与开发商签署商品房买卖合同，约定2026年5月31日交房。买方陈述已支付首付款。开发商后来提出延期交付计划，是否已实际交付尚待核实。买方发出催告，但通知到达及约定宽限期仍待核实。买方希望了解继续履行与解除合同的条件，不主张无条件停止还贷。原合同、付款流水、通知及签收记录需要核对。'})
        call(c, 'PUT', '/cases/'+case['id']+'/research-selections', {'doc_ids': ids})
        run = call(c, 'POST', '/cases/'+case['id']+'/requests', {'workflow': 'case.assess', 'goal': f'结合选定{len(ids)}个类案独立分析后综合给出有条件的诉讼路径，明确差异、待核证据与下一步。'})
        record = {'case_id': case['id'], 'run_id': run['id'], 'doc_ids': ids, **({k: runtime[k] for k in ('base', 'credentials_path')} if runtime else {})}
        save('research-live-run.json', record)
        print(json.dumps(record, ensure_ascii=False))
    else:
        record = json.loads(record_path.read_text())
        progress = c.get('/runs/'+record['run_id']+'/research').json()
        batch = progress.get('batch') or {}
        print(json.dumps({'status': progress['run_status'], 'stage': progress['stage'], 'error': progress.get('error'), 'completed': batch.get('completed'), 'total': batch.get('total')}, ensure_ascii=False))
        if progress['run_status'] == 'succeeded':
            case = c.get('/cases/'+record['case_id']).json()
            artifact = next(a for a in case['artifacts'] if a['run_id'] == record['run_id'] and a['kind'] == 'research')
            record['artifact_id'] = artifact['id']
            save('research-live-run.json', record)
            save('research-live-results.json', {'progress': progress, 'artifact': artifact})
            (ROOT/'evaluation/research-live-report.md').write_text(artifact['body'])
finally:
    c.close()
