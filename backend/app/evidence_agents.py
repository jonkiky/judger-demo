"""Durable selected-case evidence specialists followed by one synthesis agent.

Each specialist receives exactly one public judgment. A join waits for every
selected specialist; failures never become an apparently complete synthesis.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import re
from typing import Literal, Annotated
from pydantic import Field
from . import settings
from .contracts import Strict
from .db import connect, digest, dumps, event, now
from .model import generate, service


class Finding(Strict):
    evidence: str = Field(min_length=1, max_length=80)
    fact: str = Field(min_length=1, max_length=180)
    treatment: Literal["采信", "不采信", "仅记载", "无法确认"]
    treatment_quote: str = Field(default="", max_length=200)
    reasoning: str = Field(min_length=1, max_length=180)
    source_ids: list[str] = Field(min_length=1, max_length=3)


class CaseEvidence(Strict):
    findings: list[Finding] = Field(min_length=1, max_length=3)
    limits: list[Annotated[str, Field(min_length=3, max_length=120, pattern=r"^.+[？?]$")]] = Field(min_length=1, max_length=3)


class VerificationQuestion(Strict):
    question: str = Field(min_length=3, max_length=60, pattern=r"^[^。！？?!\r\n]+[？?]$")
    why_matters: str = Field(min_length=1, max_length=60, pattern=r"^[^。！？?!\r\n]+[。！!.]?$", description="一句简短说明，只写核实该问题的作用。")


class SynthesisItem(Strict):
    issue: str = Field(min_length=1, max_length=80)
    finding_ids: list[str] = Field(min_length=1, max_length=5)
    material_source_ids: list[str] = Field(max_length=5)
    verification_question: str = Field(min_length=3, max_length=60, pattern=r"^[^。！？?!\r\n]+[？?]$")
    why_matters: str = Field(min_length=1, max_length=60, pattern=r"^[^。！？?!\r\n]+[。！!.]?$", description="一句简短说明，只写核实该问题的作用。")
    next_step: str = Field(min_length=1, max_length=220)
    evidence_needed: str = Field(min_length=1, max_length=140)
    why_needed: str = Field(min_length=1, max_length=180)
    obtain_from: str = Field(min_length=1, max_length=120)


class EvidenceSynthesis(Strict):
    items: list[SynthesisItem] = Field(min_length=1, max_length=6)
    unresolved: list[VerificationQuestion] = Field(min_length=1, max_length=6)


def synthesis_case_reports(reports):
    """Transfer evidence methods, never another party's facts, into this case.

    The complete specialist reports remain visible and archived with exact
    sources. The synthesis model receives only neutral evidence categories so
    it cannot copy foreign names, contract titles, amounts or dates as facts.
    """
    categories = [
        (r"合同|协议|借条|欠条|借据|认购", "合同、协议或债权凭据"),
        (r"付款|收款|转账|流水|收据|发票|汇款|结算|支付", "款项往来及收付凭证"),
        (r"通知|催告|信函|函|签收|送达|邮寄", "通知内容及送达记录"),
        (r"照片|视频|现场|验收|交接|施工|测绘", "履行过程、现场或交接记录"),
        (r"聊天|微信|短信|邮件|录音|通话", "沟通记录及电子数据"),
        (r"登记|证照|产权|权属|资质|身份证|营业执照", "身份、权属或登记材料"),
        (r"鉴定|评估|审计", "专业意见或评估材料"),
    ]
    result = []
    for report in reports:
        findings = []
        for finding in report["findings"]:
            types = [label for pattern, label in categories if re.search(pattern, finding["evidence"])]
            findings.append({"id": finding["id"], "evidence_types": types or ["其他原始材料，类型需要核对"],
                             "treatment": finding["treatment"]})
        result.append({"doc_id": report["doc_id"], "case_number": report["case_number"], "findings": findings})
    return result


def batch_view(c, run_id):
    row = c.execute("SELECT * FROM evidence_batches WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    for key in ("candidates", "selected_doc_ids", "retrieval"):
        result[key] = json.loads(result[key])
    agents = []
    for row in c.execute("SELECT * FROM evidence_agents WHERE run_id=? ORDER BY rank", (run_id,)):
        agent = dict(row)
        for key in ("sources", "coverage", "result"):
            agent[key] = json.loads(agent[key]) if agent[key] else None
        agents.append(agent)
    result.update(agents=agents, completed=sum(a["status"] == "succeeded" for a in agents),
                  total=len(agents), max_cases=settings.EVIDENCE_MAX_CASES,
                  concurrency=settings.EVIDENCE_CONCURRENCY)
    return result


def prepare_batch(state):
    from . import workflows as w
    policy = state["snapshot"].get("evidence_policy")
    if policy and policy["version"] != settings.EVIDENCE_VERSION:
        raise ValueError("证据分析流程版本已变化，请新建分析")
    with connect() as c:
        existing = batch_view(c, state["run_id"])
    if existing:
        if existing["version"] != settings.EVIDENCE_VERSION:
            raise ValueError("证据分析流程版本已变化，请新建分析")
        return existing
    w.guard(state, "核对选定类案及筛选范围")
    review = state["snapshot"].get("evidence_review")
    selection = state["snapshot"].get("evidence_selection")
    if selection:
        candidates, retrieval = selection["candidates"], selection["retrieval"]
        selected = selection["doc_ids"]
    elif review:
        candidates, retrieval = review["candidates"], review["retrieval"]
        selected = review["doc_ids"]
    else:
        raise ValueError("此任务没有保存类案筛选范围，请在证据分析页选择类案后新建分析。")
    if not selected:
        raise ValueError("当前语料没有检索到可分析类案；未编造案例或生成空的综合结论。请调整材料或研究目标后重试。")
    if not 1 <= len(selected) <= settings.EVIDENCE_MAX_CASES or len(set(selected)) != len(selected):
        raise ValueError("证据分析每次只能分析 1 至 5 个不同类案")
    if not set(selected).issubset({item["document_id"] for item in candidates}):
        raise ValueError("所选类案不在本轮检索候选中")
    from .evidence_workspace import distinct_cases
    if len(distinct_cases([i for i in candidates if i["document_id"] in selected])) != len(selected):
        raise ValueError("同一案件只能选择一篇裁判文书")
    w.guard(state, "准备逐案证据分析")
    with connect(True) as c:
        c.execute("INSERT INTO evidence_batches(run_id,version,candidates,selected_doc_ids,retrieval,selection_mode,source_run_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (
            state["run_id"], settings.EVIDENCE_VERSION, dumps(candidates), dumps(selected), dumps(retrieval),
            selection["selection_mode"] if selection else "reviewed" if review else "automatic_top5", review["run_id"] if review else None,
            "analyzing", now(), now()))
        c.executemany("INSERT INTO evidence_agents(run_id,doc_id,rank,status) VALUES (?,?,?,'queued')",
                      [(state["run_id"], did, rank) for rank, did in enumerate(selected, 1)])
        event(c, state["run_id"], "evidence.selected", {"doc_ids": selected, "automatic": not bool(review),
              "available": len(candidates), "limit": settings.EVIDENCE_MAX_CASES, "concurrency": settings.EVIDENCE_CONCURRENCY})
        return batch_view(c, state["run_id"])


def judgment_spans(doc, rank, budget=6500):
    """Bound exact excerpts; disclose incomplete coverage for long judgments."""
    body = doc["content"]
    if not body or digest(body) != doc["sha256"]:
        raise ValueError("类案全文为空或哈希校验失败")
    if len(body) <= budget:
        intervals = [(0, len(body))]
    else:
        # Prefer evidence, fact finding and reasoning, with an identity/context span.
        windows = [(0, min(700, len(body)))]
        for token, width, last in (("本院认为", 1500, True), ("判决如下", 900, True),
                                   ("裁定如下", 900, True), ("证据", 1100, False),
                                   ("质证", 1000, False), ("查明", 1100, False), ("认定", 900, False)):
            pos = body.rfind(token) if last else body.find(token)
            if pos >= 0:
                start = max(0, pos-120)
                windows.append((start, min(len(body), start+width)))
        used, selected = set(), []
        for start, end in windows:
            for i in range(start, end):
                if len(used) >= budget:
                    break
                used.add(i)
        # If keywords are sparse, fill the remaining allowance from the beginning.
        for i in range(len(body)):
            if len(used) >= budget:
                break
            used.add(i)
        for i in sorted(used):
            if selected and i == selected[-1][1]:
                selected[-1][1] = i+1
            else:
                selected.append([i, i+1])
        intervals = selected
    spans = []
    for start, end in intervals:
        for offset in range(start, end, 800):
            stop = min(end, offset+800)
            spans.append({"id": f"J{rank}_{len(spans)+1}", "source_kind": "judgment", "doc_id": doc["id"],
                "name": doc["title"], "case_number": doc["case_number"], "court": doc["court"],
                "judgment_date": doc["judgment_date"], "section": "证据分析原文", "location": f"全文字符 {offset}–{stop}",
                "quote": body[offset:stop], "start": offset, "end": stop,
                "text_sha256": doc["sha256"], "release_id": doc["release_id"]})
    included = sum(s["end"]-s["start"] for s in spans)
    return spans, {"total_characters": len(body), "included_characters": included,
                   "partial": included < len(body), "method": "exact-evidence-windows-v1"}


def case_schema(sources):
    schema = deepcopy(CaseEvidence.model_json_schema())
    schema["$defs"]["Finding"]["properties"]["source_ids"]["items"]["enum"] = [s["id"] for s in sources]
    schema["$defs"]["Finding"]["properties"]["evidence"]["enum"] = evidence_names(sources)
    schema["properties"]["limits"]["items"]["maxLength"] = 120
    return schema


def evidence_names(sources):
    """Candidate labels are verbatim phrases in THIS judgment, not a template list."""
    names = []
    for source in sources:
        quote = source["quote"]
        matches = re.findall(r"《[^《》\n]{2,65}》", quote)
        matches += re.findall(r"[\u3400-\u9fffA-Za-z0-9（）()]{0,18}(?:凭证|收据|发票|报告|照片|记录|通知书|告知书|承诺书|证言|鉴定意见|回单|流水|不动产权证|备案表|合同|协议)", quote)
        for name in matches:
            if any(word in name for word in ("民法典", "诉讼法", "最高人民法院", "司法解释", "中华人民共和国", "法律规定")):
                continue
            if name not in names:
                names.append(name)
    return names[:60] or ["原文未载具体证据名称"]


def validate_case_result(raw, sources, rank):
    from . import workflows as w
    result = CaseEvidence.model_validate(raw).model_dump()
    allowed = {s["id"] for s in sources}
    for i, finding in enumerate(result["findings"], 1):
        if not set(finding["source_ids"]).issubset(allowed):
            raise ValueError("逐案分析引用了另一个案件或未知来源")
        if finding["evidence"] not in evidence_names(sources):
            raise ValueError("证据名称不是当前判决中的原文词组")
        # Resolve the literal evidence-name citation ourselves. The model may
        # cite a later discussion while the material's full name appears earlier.
        # This adds only an exact name occurrence, never invents support for a claim.
        if finding["evidence"] != "原文未载具体证据名称":
            mention = next(s for s in sources if finding["evidence"] in s["quote"])
            finding["evidence_source_id"] = mention["id"]
            finding["source_ids"] = list(dict.fromkeys([mention["id"], *finding["source_ids"]]))
        else:
            finding["treatment"] = "无法确认"
        # A dismissed claim or an invalid mortgage is not an evidence-admission
        # ruling. Only retain an admission label with a literal, specific court
        # statement; unresolved labels stay unresolved rather than guessing.
        if finding["treatment"] in ("采信", "不采信"):
            quote = finding["treatment_quote"]
            cited = [s["quote"] for s in sources if s["id"] in finding["source_ids"]]
            explicit = bool(quote and any(quote in text for text in cited)
                and finding["evidence"] in quote and re.search(r"本院|法院", quote))
            rejection = bool(re.search(r"不予采信|不予确认|不予认定|不采信", quote))
            acceptance = bool(re.search(r"予以采信|予以确认|予以认定", quote)) and not rejection
            if not explicit or not (rejection if finding["treatment"] == "不采信" else acceptance):
                finding["treatment"] = "无法确认"
                finding["treatment_quote"] = ""
                finding["admission_note"] = "未定位到针对该项证据的明示采信评价；实体请求是否获支持不能替代证据采信判断。"
        else:
            finding["treatment_quote"] = ""
        w.validate_paragraphs({"sections": [{"heading": "逐案证据", "paragraphs": [{
            "text": finding["evidence"]+finding["fact"]+finding["reasoning"], "source_ids": finding["source_ids"]}]}]}, sources)
        finding["id"] = f"E{rank}_{i}"
    if any(len(x) > 120 for x in result["limits"]):
        raise ValueError("逐案分析待核事项过长")
    return result


def analyze_one(state, did):
    from . import workflows as w
    rid = state["run_id"]
    w.guard(state, "逐案分析证据，等待各分析完成")
    with connect() as c:
        row = dict(c.execute("SELECT * FROM evidence_agents WHERE run_id=? AND doc_id=?", (rid, did)).fetchone())
    if row["status"] == "succeeded":
        # Re-check the durable result's exact sources before reusing it on recovery.
        with connect() as c:
            w.check_sources(c, state["snapshot"]["case"]["id"], json.loads(row["sources"]))
        return
    try:
        doc = service("GET", "/documents/"+did)
        if doc["id"] != did or doc["release_id"] != state["snapshot"]["release"]["release_id"]:
            raise ValueError("逐案来源不属于固定的语料版本")
        sources, coverage = judgment_spans(doc, row["rank"])
        payload = {"judgment": {k: doc[k] for k in ("id", "title", "case_number", "court", "judgment_date")},
                   "coverage": coverage, "sources": w.model_sources(sources), "evidence_names": evidence_names(sources)}
        input_hash = digest(payload)
        if row["input_hash"] and row["input_hash"] != input_hash:
            raise ValueError("逐案输入已变化，不能复用旧请求")
        validated_cache = None
        if row["status"] == "failed" and row["attempt"]:
            with connect() as c:
                previous = c.execute("SELECT result FROM model_calls WHERE operation_key=? AND status='succeeded'",
                                     (f"{rid}:evidence_case:{did}:{row['attempt']}",)).fetchone()
            if previous:
                try:
                    validated_cache = validate_case_result(json.loads(previous["result"]), sources, row["rank"])
                except ValueError:
                    pass
        # A running operation may have completed before a process interruption;
        # keep its key so generate() can reuse the completed response.
        attempt = row["attempt"] if row["status"] == "running" or validated_cache else row["attempt"]+1
        if attempt > 2:
            raise ValueError("本类案已达两次分析尝试上限，请新建分析")
        with connect(True) as c:
            c.execute("UPDATE evidence_agents SET status='running',attempt=?,input_hash=?,sources=?,coverage=?,error=NULL,started_at=?,finished_at=NULL WHERE run_id=? AND doc_id=?",
                      (attempt, input_hash, dumps(sources), dumps(coverage), now(), rid, did))
            event(c, rid, "evidence.agent.started", {"doc_id": did, "rank": row["rank"], "attempt": attempt, "revalidated_response": bool(validated_cache)})
        system = w.BASE_SYSTEM + "\n你是单案证据分析员。只分析这一个判决，不分析其他案例、不分析当前客户案情。evidence只从evidence_names选择一个原文名称，不能合并、扩写或截断名称。source_ids必须包括记载这个名称的片段。只提取证据材料及其证明目的和法院如何评价证据。fact写该材料用于证明什么事实，不写合同有效、应当还款、抵押权成立等实体法律结论。本阶段不输出法条条号。reasoning仅解释对证据的评价，不做诉讼策略分析。treatment只有原文明示对该证据采信/不采信才用相应值，此时treatment_quote必须逐字摘录含证据全名、法院主体、明示采信评价的原句，最长200字。支持或驳回诉讼请求、调整违约金不等于采信/不采信某项证据。无法提供这样的原句时用仅记载或无法确认，treatment_quote填空字符串。未载具体证据时选对应占位标签，不得造名。最多3条，fact和reasoning各尽量60字以内。limits只能提出1至3个核对原文和证据关系的问题，每条以问号结尾；不得断言判决未载某事，不得否认原文已经认定的事实。返回findings（evidence、fact、treatment、treatment_quote、reasoning、source_ids）和limits。"
        if validated_cache:
            result = validated_cache
        else:
            raw = generate(f"{rid}:evidence_case:{did}:{attempt}", system, payload, rid, 1500, output_schema=case_schema(sources))
            result = validate_case_result(raw, sources, row["rank"])
        w.guard(state, "收集逐案证据分析结果")
        with connect(True) as c:
            c.execute("UPDATE evidence_agents SET status='succeeded',result=?,error=NULL,finished_at=? WHERE run_id=? AND doc_id=?", (dumps(result), now(), rid, did))
            completed = c.execute("SELECT count(*) FROM evidence_agents WHERE run_id=? AND status='succeeded'", (rid,)).fetchone()[0]
            total = c.execute("SELECT count(*) FROM evidence_agents WHERE run_id=?", (rid,)).fetchone()[0]
            c.execute("UPDATE runs SET stage=?,updated_at=? WHERE id=?", (f"逐案分析已完成 {completed}/{total}，等待汇总", now(), rid))
            event(c, rid, "evidence.agent.completed", {"doc_id": did, "rank": row["rank"], "completed": completed, "total": total})
    except Exception as exc:
        with connect(True) as c:
            c.execute("UPDATE evidence_agents SET status='failed',error=?,finished_at=? WHERE run_id=? AND doc_id=?", (str(exc)[:500], now(), rid, did))
            event(c, rid, "evidence.agent.failed", {"doc_id": did, "error": str(exc)[:300]})
        raise


def synthesize(state, batch):
    from . import workflows as w
    if not batch["agents"] or any(a["status"] != "succeeded" for a in batch["agents"]):
        raise ValueError("必须收到所有逐案分析后才能汇总")
    w.guard(state, "全部逐案响应已收齐，综合分析本案证据")
    material_sources = [s for s in state["sources"] if s["source_kind"] == "case_material"]
    facts = w.bounded_facts(state, 3500)
    reports, all_findings, sources = [], {}, list(material_sources)
    for agent in batch["agents"]:
        item = next(c for c in batch["candidates"] if c["document_id"] == agent["doc_id"])
        reports.append({"doc_id": agent["doc_id"], "case_number": item["case_number"],
                        "findings": agent["result"]["findings"], "limits": agent["result"]["limits"], "coverage": agent["coverage"]})
        all_findings.update({f["id"]: {**f, "doc_id": agent["doc_id"]} for f in agent["result"]["findings"]})
        sources.extend(agent["sources"])
    system = w.BASE_SYSTEM + "\n你是证据分析统筹员。所有逐案分析已收齐，现在综合全部分析，映射到当前本案的证据核验任务。每个案例至少引用一个finding_id；按共同证据主题归纳，保留相反或不利证据的核对方向。不得重新复述或改写类案的事实和裁判结论；类案要点由程序按finding_id原样关联。不得判定本案有无某证据、某方已违约或违法，不作实体法律结论。items写3至6项，每项为issue（简短证据主题名称）、finding_ids、material_source_ids、verification_question、next_step。verification_question只写需要核验的问题，必须以问号结尾，例如询问本案原始合同约定、通知到达或资金流水如何证明相关事实；不得把未知情况预设为不存在、未提供、未办理。next_step具体写向谁取得何种材料以及证明目的，尽量80字以内；异地案例的特定证照名称或交付要求不得直接当成本案必需材料，应先核对本案合同与所在地适用要求。只能复制已提供ID。unresolved写1至6个核验问题，每个必须以问号结尾，不写无依据的缺失断言。引用判决不是证明本案事实。"
    system += "\n每项还必须包含evidence_needed（建议补充或核对的具体证据）、why_needed（为何需要、用于证明什么、与本案争点的关系）、obtain_from（向谁或哪个渠道取得）。已有关联材料时说明需要核对原件、补充哪些细节，不能声称该材料缺失。verification_question与unresolved合计只提出至多10个不重复的简短问题。"
    system += "\n输出结构以本条为准：返回evidence_by_case和unresolved。按照required_case_coverage的每个case键各写一项本案证据核验任务，finding_ids只能引用该键指定的结论。所有键都必须完成，不得用同一案例替代其他案例。case_reports仅提供类案证据类型和评价状态，具体原文由程序关联。各项围绕case_facts的事实及provided_materials，尽量覆盖不同证据主题；不得虚构本案合同名称、当事人、交易、金额或日期。类案证据方法仅供参考，不能套用其合同义务或证据要求。why_needed只说明拟核实的事实，不宣称法律规定某证据必需、某程序已满足或某法律结论成立。"
    system += "\n待核问题只写问题和为何重要：每项verification_question只问一个问题，另写why_matters说明核实该问题的作用；两者各用一句短句，各不超过60字，尽量30字以内。不要添加背景、分析过程或长段落。unresolved改为对象数组，每项只含question和why_matters，同样遵守短句要求。为何重要只说明核对事实的用途，不作法律结论。"
    coverage = {f"case_{i+1}": {"doc_id": report["doc_id"], "case_number": report["case_number"],
                "finding_ids": [finding["id"] for finding in report["findings"]]}
                for i, report in enumerate(reports)}
    payload = {"case": {k: state["snapshot"]["case"][k] for k in ("title", "position", "claims")},
               "provided_materials": [{"name": m["name"], "kind": m["kind"]} for m in w.get_materials(state)],
               "case_facts": [{"id": f["id"], "quote": f["quote"],
                               **({"user_description": f["description"], "status": f["status"]} if "description" in f else {})} for f in facts],
               "omitted_fact_count": len(state["facts"])-len(facts), "case_reports": synthesis_case_reports(reports),
               "selected_cases": batch["total"], "selection_mode": batch["selection_mode"],
               "required_case_coverage": coverage}
    while len(system)+len(dumps(payload)) > 14000 and payload["case_facts"]:
        payload["case_facts"].pop()
        payload["omitted_fact_count"] += 1
    if len(system)+len(dumps(payload)) > 14000:
        raise ValueError("全部逐案分析超过汇总上下文预算，未静默丢弃任何案例")
    shown_material = {f["id"] for f in payload["case_facts"]}
    schema = deepcopy(EvidenceSynthesis.model_json_schema())
    props = schema["$defs"]["SynthesisItem"]["properties"]
    props["finding_ids"]["items"]["enum"] = list(all_findings)
    if shown_material:
        props["material_source_ids"]["items"]["enum"] = sorted(shown_material)
    else:
        props["material_source_ids"]["maxItems"] = 0
    # Required object keys and per-case finding enums make omission impossible
    # during constrained generation, while validation still checks the result.
    tasks = {}
    for key, case_coverage in coverage.items():
        task = deepcopy(schema["$defs"]["SynthesisItem"])
        task["properties"]["finding_ids"]["items"]["enum"] = case_coverage["finding_ids"]
        tasks[key] = task
    schema["properties"].pop("items")
    schema["properties"]["evidence_by_case"] = {"type": "object", "properties": tasks,
        "required": list(tasks), "additionalProperties": False}
    schema["required"] = ["evidence_by_case", "unresolved"]
    with connect(True) as c:
        current = c.execute("SELECT status,synthesis_attempt FROM evidence_batches WHERE run_id=?", (state["run_id"],)).fetchone()
        attempt = current["synthesis_attempt"] if current["status"] == "synthesizing" else current["synthesis_attempt"]+1
        if attempt > 2:
            raise ValueError("统筹分析已达两次尝试上限，请新建分析；逐案结果已保留")
        c.execute("UPDATE evidence_batches SET status='synthesizing',synthesis_attempt=?,updated_at=? WHERE run_id=?", (attempt, now(), state["run_id"]))
        event(c, state["run_id"], "evidence.synthesis.started", {"responses_received": batch["completed"], "expected": batch["total"]})
    raw = generate(state["run_id"]+f":evidence_synthesis:{attempt}", system, payload, state["run_id"], 3500, output_schema=schema)
    if "evidence_by_case" in raw:
        entries = raw["evidence_by_case"]
        if not isinstance(entries, dict) or set(entries) != set(coverage):
            raise ValueError("统筹分析遗漏了选定案例，未交付不完整汇总")
        for key, entry in entries.items():
            if not set(entry.get("finding_ids", [])).issubset(coverage[key]["finding_ids"]):
                raise ValueError("统筹分析将其他案例结论混入当前案例")
        raw = {"items": [entries[key] for key in coverage], "unresolved": raw["unresolved"]}
    result = EvidenceSynthesis.model_validate(raw).model_dump()
    covered, items = set(), []
    for item in result["items"]:
        if not set(item["finding_ids"]).issubset(all_findings) or not set(item["material_source_ids"]).issubset(shown_material):
            raise ValueError("统筹分析引用了未提供的逐案结论或本案材料")
        chosen = [all_findings[fid] for fid in item["finding_ids"]]
        covered.update(f["doc_id"] for f in chosen)
        refs = list(dict.fromkeys([*item["material_source_ids"], *(sid for f in chosen for sid in f["source_ids"])]))
        w.validate_paragraphs({"sections": [{"heading": item["issue"], "paragraphs": [{
            "text": item["issue"]+item["verification_question"]+item["why_matters"]+item["next_step"]+item["evidence_needed"]+item["why_needed"]+item["obtain_from"], "source_ids": refs}]}]}, sources)
        basis = (f"本轮材料已定位 {len(item['material_source_ids'])} 条相关记载；记载与原件是否一致及其证明力仍待核验。"
                 if item["material_source_ids"] else "本轮汇总未关联本案材料片段，不能据此认定相应事实或证据不存在。")
        names = "、".join(dict.fromkeys(f["evidence"] for f in chosen))
        gap = f"关联 {len({f['doc_id'] for f in chosen})} 个类案的证据要点：{names}。{basis}\n核验问题：{item['verification_question']}"
        items.append({**item, "gap": gap, "source_ids": refs,
                      "status": "类案对照建议 · 待核验", "basis_type": "comparative_practice_suggestion"})
    if covered != set(batch["selected_doc_ids"]):
        raise ValueError("统筹分析遗漏了选定案例，未交付不完整汇总")
    limits = ["公共判决不能作为本案事实已经成立的证明；具体证据及证明力须由律师核对。"]
    if batch["total"] < settings.DEFAULT_CASE_ANALYSIS_COUNT:
        limits.append(f"本轮仅分析 {batch['total']} 个实际选定类案。")
    if any(a["coverage"]["partial"] for a in batch["agents"]):
        limits.append("部分长判决仅分析精确节选，未宣称完成全文审阅；应打开完整原文核对。")
    if payload["omitted_fact_count"]:
        limits.append(f"本轮汇总上下文未纳入 {payload['omitted_fact_count']} 条材料摘录，不能据此认定相应事实或证据不存在。")
    from .evidence_questions import question_details
    details = question_details([{"question": i["verification_question"], "why_matters": i["why_matters"]} for i in items]+result["unresolved"])
    questions = [item["question"] for item in details]
    notices = [s for s in limits if not s.rstrip().endswith(("？", "?"))]
    structured = {"items": items, "unresolved": list(dict.fromkeys([*questions, *notices])),
                  "follow_up_questions": questions, "follow_up_details": details, "notices": notices,
                  "rule_status": "unreviewed_practice_suggestions", "multi_agent": {
                      "version": settings.EVIDENCE_VERSION, "selection_mode": batch["selection_mode"],
                      "selected_doc_ids": batch["selected_doc_ids"], "completed": batch["completed"], "total": batch["total"],
                      "candidate_count": len(batch["candidates"]), "source_run_id": batch["source_run_id"],
                      "case_reports": reports, "omitted_fact_count": payload["omitted_fact_count"]}}
    from .evidence_workspace import evidence_view
    view = evidence_view({"structured": structured, "sources": sources})
    for entry in view["existing_evidence"]:
        material = next((m for m in w.get_materials(state) if m["id"] == entry["material_id"]), None)
        if material:
            entry["kind"] = material["kind"]
    structured["existing_evidence"] = view["existing_evidence"]
    body = "# 证据分析与待核事项\n\n" + f"已收齐 {batch['total']} 个选定类案的独立分析，以下对照本案材料提出补证与核验建议。\n\n## 已有证据与材料\n\n"
    body += "\n\n".join(e["name"]+" · "+e["status"]+" "+" ".join("["+sid+"]" for sid in e["source_ids"]) for e in view["existing_evidence"])
    body += "\n\n## 建议补充或核对的证据\n\n"
    body += "\n\n".join(f"### {i['issue']}\n\n证据：{i['evidence_needed']}\n\n为何需要：{i['why_needed']}\n\n获取渠道：{i['obtain_from']}\n\n下一步：{i['next_step']}\n\n依据："+" ".join("["+x+"]" for x in i["source_ids"]) for i in items)
    body += "\n\n## 待核问题\n\n"+"\n".join(f"- {item['question']} 为何重要：{item['why_matters']}" for item in details)
    body += "\n\n## 分析范围说明\n\n"+"\n".join(notices)
    aid = w.save(state, "evidence", "证据分析与待核事项", body, structured, sources)
    with connect(True) as c:
        c.execute("UPDATE evidence_batches SET status='succeeded',updated_at=? WHERE run_id=?", (now(), state["run_id"]))
        event(c, state["run_id"], "evidence.synthesis.completed", {"artifact_id": aid, "cases": batch["total"]})
    return {"gaps": items, "outputs": [*state["outputs"], aid]}


def run_evidence(state):
    batch = prepare_batch(state)
    if batch["status"] == "succeeded":
        from . import workflows as w
        with connect() as c:
            old = c.execute("SELECT id,structured,sources FROM artifacts WHERE operation_key=?", (state["run_id"]+":evidence",)).fetchone()
            if old:
                w.check_sources(c, state["snapshot"]["case"]["id"], json.loads(old["sources"]))
                return {"gaps": json.loads(old["structured"])["items"], "outputs": [*state["outputs"], old["id"]]}
    errors = []
    try:
        with ThreadPoolExecutor(max_workers=settings.EVIDENCE_CONCURRENCY, thread_name_prefix="evidence-case") as pool:
            futures = {pool.submit(analyze_one, state, did): did for did in batch["selected_doc_ids"]}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    errors.append((futures[future], str(exc)))
        with connect() as c:
            batch = batch_view(c, state["run_id"])
        if errors or batch["completed"] != batch["total"]:
            failed = "；".join(did+"："+error[:120] for did, error in errors)
            raise ValueError(f"逐案分析完成 {batch['completed']}/{batch['total']}，尚未汇总。成功结果已保存，可恢复未完成请求。{failed}")
        return synthesize(state, batch)
    except Exception:
        with connect(True) as c:
            cancelled = c.execute("SELECT cancel_requested FROM runs WHERE id=?", (state["run_id"],)).fetchone()[0]
            c.execute("UPDATE evidence_batches SET status=?,updated_at=? WHERE run_id=?", ("cancelled" if cancelled else "failed", now(), state["run_id"]))
        raise
