"""Isolated evaluation of an already-installed model; does not change runtime approval."""
import json
import sys
import time
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.workflows import model_sources, BASE_SYSTEM, validate_paragraphs
from scripts.evaluation_client import ROOT, save

def main():
 artifact=json.loads((ROOT/'evaluation/strategy-4b-raw.json').read_text())
 sources=artifact['sources']
 system=BASE_SYSTEM+'''只使用给定原文，认真逐条阅读适用前提和裁判主文。
返回 JSON {"sections":[{"heading":"标题","paragraphs":[{"text":"内容","source_ids":["M1"]}]}],"unresolved":["待核事项"]}。
仅写三节：1.诉求的成立条件及诉讼参与人、合并或分案路径；2.金钱请求，按付款义务人→受领人逐笔说明金额、口径和前提；3.期间、通知效力与管辖的待核条件。
履行宽限期、解除权行使期间、诉讼时效是不同概念。未知到达日期不推算确定截止日。
已付款与未付余额须分别追踪资金流，按给定裁判主文与法条理解，不能无依据改变受领人。
原件或事实未知就保留未知；不得因对方不同意就推断权利不存在；不借用未给定的法条或将假设写成事实。每节1至2段，简洁具体。'''
 payload={'sources':model_sources(sources),'goal':'从我方当事人代理律师立场制定有条件的诉讼策略。'}
 request={'model':'qwen3:8b','stream':False,'think':False,'format':'json','messages':[{'role':'system','content':system},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],'options':{'temperature':0,'num_ctx':16384,'num_predict':2500}}
 save('model-comparison/8b-request.json',request)
 started=time.monotonic()
 r=httpx.post('http://127.0.0.1:11434/api/chat',json=request,timeout=300,trust_env=False);r.raise_for_status();d=r.json()
 save('model-comparison/8b-response.json',d)
 value=json.loads(d['message']['content']);validate_paragraphs(value,sources)
 tags=httpx.get('http://127.0.0.1:11434/api/tags',trust_env=False).json()['models']
 report={'model':'qwen3:8b','digest':next(m['digest'] for m in tags if m['name']=='qwen3:8b'),'elapsed_seconds':round(time.monotonic()-started,2),'finish_reason':d.get('done_reason'),'source_ids_valid':True,'production_approved':False,'result':value}
 save('model-comparison/8b-evaluation.json',report)
 print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
