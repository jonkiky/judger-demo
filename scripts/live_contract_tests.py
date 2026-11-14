"""Read-only integration checks against the running services and the real 50k release."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
import httpx
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.evaluation_client import client,call,save,ROOT
from backend.app import settings

def main():
 p=argparse.ArgumentParser();p.add_argument('--embedding',action='store_true');args=p.parse_args()
 c=client();checks=[]
 result=call(c,'POST','/search',{'query':'(2025)粤1303民初9812号','mode':'case_number'})
 assert len(result['items'])==1
 item=result['items'][0];doc=call(c,'GET','/judgments/'+item['document_id'])
 assert hashlib.sha256(doc['content'].encode()).hexdigest()==doc['sha256']
 assert doc['content'][item['start']:item['end']]==item['quote']
 checks.append('real_document_hash_and_exact_quote')
 filters={k:doc[k] for k in ['province','court','cause']}
 filters.update(date_from=doc['judgment_date'],date_to=doc['judgment_date'])
 exact=call(c,'POST','/search',{'query':doc['case_number'],'mode':'case_number',**filters})
 assert len(exact['items'])==1
 zero=call(c,'POST','/search',{'query':doc['case_number'],'mode':'case_number',**{**filters,'province':'山东省'}})
 assert zero['items']==[] and zero['applied_filters']['province']=='山东省'
 checks.append('all_exact_filters_preserved')
 zero=call(c,'POST','/search',{'query':'商品房买卖合同解除','mode':'hybrid','province':'不存在的省份'})
 assert zero['items']==[] and not zero['degraded']
 checks.append('zero_results_never_widen_filters')
 lexical=call(c,'POST','/search',{'query':'逾期交付 贷款','mode':'keyword','province':'广东省','limit':4})
 for it in lexical['items']:
  full=call(c,'GET','/judgments/'+it['document_id'])
  assert full['province']=='广东省' and all(s in full['content'] for s in ['逾期交付','贷款'])
 checks.append('keyword_and_province_rechecked_in_fulltext')
 with httpx.Client(trust_env=False) as anonymous:
  assert anonymous.get('http://127.0.0.1:8765/api/v1/judgments/'+item['document_id']).status_code==401
  assert anonymous.get('http://127.0.0.1:8767/health').status_code==401
  assert anonymous.get('http://127.0.0.1:8766/health').status_code==401
 checks.append('public_and_internal_services_require_credentials')
 report={'checks':checks,'release_id':result['release_id'],'selected_case_number':doc['case_number'],'selected_court':doc['court'],'keyword_results':len(lexical['items'])}
 if args.embedding:
  query=settings.QUERY_INSTRUCTION+'房屋租赁 合同解除 押金返还'
  headers={'X-Internal-Key':settings.internal_key()}
  direct=httpx.post(settings.OLLAMA+'/api/embed',json={'model':settings.EMBED_MODEL,'input':[query],'dimensions':512,'truncate':True},timeout=150,trust_env=False)
  direct.raise_for_status();raw=np.asarray(direct.json()['embeddings'][0],dtype=np.float32);raw=raw/np.linalg.norm(raw)
  proxy=httpx.post(settings.GATEWAY+'/v1/embeddings',json={'model':settings.EMBED_ALIAS,'input':query},headers=headers,timeout=150,trust_env=False)
  proxy.raise_for_status();vec=np.asarray(proxy.json()['data'][0]['embedding'],dtype=np.float32)
  cosine=float(np.dot(raw,vec)/(np.linalg.norm(raw)*np.linalg.norm(vec)))
  assert len(vec)==512 and cosine>.99999 and np.max(np.abs(raw-vec))<1e-5
  report['embedding']={'dimensions':len(vec),'cosine':cosine,'max_absolute_difference':float(np.max(np.abs(raw-vec))),'same_query_instruction':True,'original_truncate':True,'gateway_truncate':False,'note':'Short supported query; gateway rejects oversized input rather than silently truncating.'}
  checks.append('litellm_embedding_matches_original_ollama_profile')
 spec=httpx.get('http://127.0.0.1:8765/openapi.json',trust_env=False).json()
 (ROOT/'docs').mkdir(exist_ok=True)
 (ROOT/'docs/openapi.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2))
 save('live-contract-results.json',report)
 print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
