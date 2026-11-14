"""Generate a representative layout fixture with the production Word builder."""
from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from backend.app.evidence_documents import build_reference
sources=[{'id':'M1','name':'合同与付款材料摘要','source_kind':'case_material','doc_id':'material-layout','location':'段落 1',
    'quote':'本示例材料记载双方约定房屋交付日期，现有材料为合同摘要，应核对合同原件中的完整条款。'},
    {'id':'J1_1','name':'商品房交付纠纷示例裁判','source_kind':'judgment','doc_id':'judgment-layout','location':'全文字符 0 至 800',
    'quote':'法院对证据的评价需要结合完整文书核对。合同、交付通知及送达记录之间的对应关系应当分别核查。'*16}]
view={'existing_evidence':[{'name':'合同与付款材料摘要','status':'已提供材料 原件及证明力待核验','issues':['合同约定与实际履行'], 'source_ids':['M1']}],
    'additional_evidence':[{'issue':'合同约定与实际履行','evidence_needed':'完整合同、补充协议及交付通知原件','why_needed':'核对交付日期、交付条件与违约责任的完整约定，并确认摘要是否准确。','obtain_from':'向当事人核对已保存的合同、补充协议及通知文件。','next_step':'按签署顺序整理合同与补充协议，逐一核对文件签署页、交付条款与附件。','source_ids':['M1','J1_1']}],
    'follow_up_details':[
        {'question':'是否可以提供合同原件及全部附件？','why_matters':'核实合同条款是否完整。'},
        {'question':'通知的送达记录如何与具体文件对应？','why_matters':'核实通知内容及到达情况。'},
        {'question':'是否有双方后续修改约定的记录？','why_matters':'确认最终约定内容。'}],
    'notices':['公共判决只能提供类案研究线索，具体证据及证明力仍需结合本案原件核对。']}
artifact={'id':'layout-verification','version':1,'created_at':'2026-11-07T16:00:00+00:00','sources':sources,'evidence_view':view}
batch={'candidates':[{'document_id':'judgment-layout','title':'商品房交付纠纷示例裁判','case_number':'示例案号','court':'示例法院','judgment_date':'2025-06-01'}],
    'agents':[{'doc_id':'judgment-layout','rank':1,'coverage':{'partial':True},'result':{'findings':[{'evidence':'合同与交付通知','fact':'用于核对交付约定与通知内容','treatment':'无法确认','reasoning':'需结合完整判决原文及证据内容进一步核对。','source_ids':['J1_1']}]}}]}
answers=[{'created_at':'2026-11-07T16:05:00+00:00','structured':{'question':'为什么需要核对合同原件？','paragraphs':[{'text':'合同摘要可以帮助定位争点，原件及附件有助于核对条款上下文、签署信息以及是否存在修改约定。','source_ids':['M1']}],'limitations':['上述建议以当前材料记载为依据，尚未确认文件真实性。']},'sources':[sources[0]]}]
out=Path(sys.argv[1]) if len(sys.argv)>1 else root/'evaluation/evidence-reference-layout.docx'
out.write_bytes(build_reference({'title':'房屋交付纠纷证据核验示例'},artifact,batch,answers,'2026-11-07T16:10:00+00:00','America/New_York'))
print(out)
