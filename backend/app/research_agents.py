"""Independent judgment specialists, an all-results barrier, and a research editor.

Shares evidence's exact excerpt resolver, source checks, model-call cache and
bounded concurrency. Durable research rows keep the two workflows independent.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
from pydantic import Field
from . import settings
from .contracts import Strict
from .db import connect, digest, dumps, event, now
from .evidence_agents import judgment_spans
from .evidence_workspace import distinct_cases
from .model import generate, service


class ResearchFinding(Strict):
    issue: str = Field(min_length=1, max_length=80)
    facts: str = Field(min_length=1, max_length=180)
    reasoning: str = Field(min_length=1, max_length=220)
    outcome: str = Field(min_length=1, max_length=180)
    source_ids: list[str] = Field(min_length=1, max_length=4)


class CaseResearch(Strict):
    findings: list[ResearchFinding] = Field(min_length=1, max_length=2)
    limits: list[str] = Field(min_length=1, max_length=3)


class ResearchParagraph(Strict):
    text: str = Field(min_length=1, max_length=900)
    source_ids: list[str] = Field(min_length=1, max_length=8)


def validate_report(state, result, sources, operation):
    """Repair against the original sources, retaining every named case's context."""
    from . import workflows as w
    repairs = 0
    for si, section in enumerate(result.get("sections", [])):
        for pi, paragraph in enumerate(section.get("paragraphs", [])):
            try:
                w.validate_paragraphs({"sections": [{"heading": section["heading"], "paragraphs": [paragraph]}]}, sources)
            except ValueError as exc:
                if (str(exc) != "段落包含所引用原文未支持的法条条号" and not isinstance(exc, w.CaseNumberSourceError)) or repairs >= 2:
                    raise
                articles = w.article_numbers(paragraph["text"])
                numbers = w.case_numbers(paragraph["text"])
                relevant = [s for s in sources if s["id"] in paragraph["source_ids"] or
                            s["source_kind"] == "case_material" or
                            w.normalize_case_number(s.get("case_number", "")) in numbers or
                            bool(numbers & w.case_numbers(s.get("quote", ""))) or
                            (s["source_kind"] == "law" and articles & w.article_numbers(s["name"]+s["quote"]))]
                payload = {"paragraph": paragraph, "sources": w.model_sources(relevant), "error": str(exc)}
                if len(dumps(payload)) > 12000:
                    raise ValueError("引用修复超过上下文预算，请缩小研究范围")
                schema = ResearchParagraph.model_json_schema()
                schema["properties"]["source_ids"]["items"]["enum"] = [s["id"] for s in relevant]
                # Version the repair key so recovery can reuse the original
                # synthesis without colliding with the old, incomplete payload.
                raw = generate(operation+f":citation_repair:v2:{si}:{pi}", w.BASE_SYSTEM+
                    "\n你是引用核查员。按error核对当前段落，只根据给定原文修正引用与必要限定，保留有依据内容，不新增结论。正文每个法条条号必须由source_ids中对应的法律原文支持；提到类案时引用对应判决ID，本案事实引用材料ID。案号只能复制给定判决元数据或所引用原文明示的案号。若无相应原文，删除无依据条号、案号和相关结论并说明待核实，不以其他案件替换。返回text和source_ids。", payload, state["run_id"], 1200, output_schema=schema)
                replacement = ResearchParagraph.model_validate(raw).model_dump()
                try:
                    w.validate_paragraphs({"sections": [{"heading": section["heading"], "paragraphs": [replacement]}]}, relevant)
                except ValueError as remaining:
                    if str(remaining) != "段落包含所引用原文未支持的法条条号" and not isinstance(remaining, w.CaseNumberSourceError):
                        raise
                    # One final, bounded correction. Keep the source set and
                    # independently validate again; never guess a replacement ID.
                    retry_payload = {**payload, "paragraph": replacement, "error": str(remaining)}
                    if len(dumps(retry_payload)) > 12000:
                        raise ValueError("引用修复超过上下文预算，请缩小研究范围")
                    retry = generate(operation+f":citation_repair:v2:{si}:{pi}:retry", w.BASE_SYSTEM+
                        "\n这是该段最后一次引用核对。上次修复仍未通过，详见error。仅根据sources重写有依据的内容。正文不再写法条条号，使用准确的source_ids引用给定法律原文；不要把引用编号当作正文。没有对应原文的案号及相关结论应删除并说明无法核实，不得改成另一个案号。保留有依据的限定与不利事项。本案事实和类案观点分别引用材料与判决来源。返回text和source_ids。", retry_payload, state["run_id"], 1200, output_schema=schema)
                    replacement = ResearchParagraph.model_validate(retry).model_dump()
                    w.validate_paragraphs({"sections": [{"heading": section["heading"], "paragraphs": [replacement]}]}, relevant)
                section["paragraphs"][pi] = replacement
                repairs += 1
    return w.validate_paragraphs(result, sources)


def batch_view(c, rid):
    row = c.execute("SELECT * FROM research_batches WHERE run_id=?", (rid,)).fetchone()
    if not row:
        return None
    batch = dict(row)
    for key in ("candidates", "selected_doc_ids"):
        batch[key] = json.loads(batch[key])
    batch["agents"] = []
    for row in c.execute("SELECT * FROM research_agents WHERE run_id=? ORDER BY rank", (rid,)):
        agent = dict(row)
        for key in ("sources", "coverage", "result"):
            agent[key] = json.loads(agent[key]) if agent[key] else None
        batch["agents"].append(agent)
    batch.update(total=len(batch["agents"]), completed=sum(a["status"] == "succeeded" for a in batch["agents"]), max_cases=5)
    return batch


def validate_selection(ids, candidates=None):
    if not isinstance(ids, list) or not 1 <= len(ids) <= 5 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("每轮研究请选择 1 至 5 个不同类案")
    if candidates is not None:
        chosen = [i for i in candidates if i["document_id"] in ids]
        if len(chosen) != len(ids) or len(distinct_cases(chosen)) != len(ids):
            raise ValueError("请选择本轮候选中的不同案件，同一案号不能重复分析")


def prepare_batch(state):
    from . import workflows as w
    with connect() as c:
        old = batch_view(c, state["run_id"])
    if state["snapshot"].get("research_policy", settings.RESEARCH_VERSION) != settings.RESEARCH_VERSION or (old and old["version"] != settings.RESEARCH_VERSION):
        raise ValueError("研究流程已更新，请新建研究任务")
    if old:
        return old
    w.guard(state, f"固定本轮研究类案（默认 {settings.DEFAULT_CASE_ANALYSIS_COUNT} 个，最多 5 个）")
    ids = state.get("selected") or state["snapshot"].get("selected_doc_ids", [])
    candidates = []
    if ids:
        validate_selection(ids)
        for did in ids:
            doc = service("GET", "/documents/"+did)
            if doc["id"] != did or doc["release_id"] != state["snapshot"]["release"]["release_id"]:
                raise ValueError("类案与本轮语料快照不一致")
            candidates.append({"document_id": did, **{k: doc.get(k, "") for k in ("title", "case_number", "court", "judgment_date")}})
        mode = state["snapshot"].get("research_selection", {}).get("selection_mode", "selected")
    else:
        saved = state["snapshot"].get("research_candidates")
        candidates = distinct_cases(saved if saved is not None else w.retrieve_cases(state, "research_search")["candidates"])
        ids = [i["document_id"] for i in candidates[:settings.DEFAULT_CASE_ANALYSIS_COUNT]]
        mode = "filtered_default" if saved is not None else "automatic_default"
    validate_selection(ids, candidates)
    with connect(True) as c:
        c.execute("INSERT INTO research_batches(run_id,version,candidates,selected_doc_ids,selection_mode,status,created_at,updated_at) VALUES (?,?,?,?,?,'analyzing',?,?)",
                  (state["run_id"], settings.RESEARCH_VERSION, dumps(candidates), dumps(ids), mode, now(), now()))
        c.executemany("INSERT INTO research_agents(run_id,doc_id,rank,status) VALUES (?,?,?,'queued')", [(state["run_id"], did, i) for i, did in enumerate(ids, 1)])
        event(c, state["run_id"], "research.selected", {"doc_ids": ids, "selection_mode": mode, "limit": 5})
        return batch_view(c, state["run_id"])


def validate_result(raw, sources, rank):
    from . import workflows as w
    result = CaseResearch.model_validate(raw).model_dump()
    for i, finding in enumerate(result["findings"], 1):
        w.validate_paragraphs({"sections": [{"heading": finding["issue"], "paragraphs": [{"text": " ".join(finding[k] for k in ("issue", "facts", "reasoning", "outcome")), "source_ids": finding["source_ids"]}]}]}, sources)
        finding["id"] = f"R{rank}_{i}"
    if any(len(s) > 160 for s in result["limits"]):
        raise ValueError("逐案研究的范围说明过长")
    return result


def analyze_one(state, did):
    from . import workflows as w
    rid = state["run_id"]
    w.guard(state, "独立分析各类案的事实、理由与裁判结果")
    with connect() as c:
        row = dict(c.execute("SELECT * FROM research_agents WHERE run_id=? AND doc_id=?", (rid, did)).fetchone())
    if row["status"] == "succeeded":
        with connect() as c:
            w.check_sources(c, state["snapshot"]["case"]["id"], json.loads(row["sources"]))
        return
    try:
        doc = service("GET", "/documents/"+did)
        if doc["id"] != did or doc["release_id"] != state["snapshot"]["release"]["release_id"]:
            raise ValueError("类案与本轮语料版本不一致")
        sources, coverage = judgment_spans(doc, row["rank"])
        payload = {"judgment": {k: doc[k] for k in ("id", "title", "case_number", "court", "judgment_date")}, "coverage": coverage, "sources": w.model_sources(sources)}
        hashed = digest(payload)
        if row["input_hash"] and row["input_hash"] != hashed:
            raise ValueError("类案原文已变化，不能复用原请求")
        attempt = row["attempt"] if row["status"] == "running" else row["attempt"] + 1
        if attempt > 2:
            raise ValueError("该类案已达两次尝试上限，请新建研究")
        with connect(True) as c:
            c.execute("UPDATE research_agents SET status='running',attempt=?,input_hash=?,sources=?,coverage=?,error=NULL,started_at=?,finished_at=NULL WHERE run_id=? AND doc_id=?", (attempt, hashed, dumps(sources), dumps(coverage), now(), rid, did))
            event(c, rid, "research.agent.started", {"doc_id": did, "attempt": attempt})
        schema = deepcopy(CaseResearch.model_json_schema())
        schema["$defs"]["ResearchFinding"]["properties"]["source_ids"]["items"]["enum"] = [s["id"] for s in sources]
        system = w.BASE_SYSTEM + "\n你是独立的单案研究员。只分析当前这一篇判决，不讨论客户案情或其他案件。提取1至2个要点，每项包含issue争点、facts法院查明的事实、reasoning法院理由、outcome主文结果和source_ids。区分当事人主张与法院认定，支持部分请求不能概括为全案胜诉。材料未记载的结论明确写无法确认，不推断不存在。每项引用支持该要点的原文ID，法条条号只有引用原文明示时才能写。limits说明节选与适用范围限制，每项160字以内。"
        raw = generate(f"{rid}:research_case:{did}:{attempt}", system, payload, rid, 1600, output_schema=schema)
        result = validate_result(raw, sources, row["rank"])
        w.guard(state, "收集逐案研究结果")
        with connect(True) as c:
            c.execute("UPDATE research_agents SET status='succeeded',result=?,error=NULL,finished_at=? WHERE run_id=? AND doc_id=?", (dumps(result), now(), rid, did))
            event(c, rid, "research.agent.completed", {"doc_id": did})
    except Exception as exc:
        with connect(True) as c:
            c.execute("UPDATE research_agents SET status='failed',error=?,finished_at=? WHERE run_id=? AND doc_id=?", (str(exc)[:500], now(), rid, did))
            event(c, rid, "research.agent.failed", {"doc_id": did, "error": str(exc)[:300]})
        raise


def report_body(result):
    from .workflows import markdown_sections
    return "# 类案研究与诉讼策略\n\n" + markdown_sections(result["sections"]) + "\n\n## 待核事项\n\n" + "\n".join("- "+s for s in result.get("unresolved", []))


def complete_case_coverage(state, result, sources, reports, operation):
    """One bounded coordinator request for omitted comparisons, then revalidate."""
    from . import workflows as w
    cited = {sid for section in result["sections"] for p in section["paragraphs"] for sid in p["source_ids"]}
    covered = {s["doc_id"] for s in sources if s["id"] in cited and s["source_kind"] == "judgment"}
    missing = [r for r in reports if r["doc_id"] not in covered]
    if not missing:
        return
    materials = [s for s in sources if s["source_kind"] == "case_material"]
    payload = {"case": {k: state["snapshot"]["case"][k] for k in ("title", "position", "claims")}, "case_reports": missing, "material_sources": w.model_sources(materials)}
    while len(dumps(payload)) > 12000 and payload["material_sources"]:
        payload["material_sources"].pop()
    if len(dumps(payload)) > 12000:
        raise ValueError("补充类案比较超过上下文预算，未省略选定案例")
    properties = {}
    material_ids = [s["id"] for s in payload["material_sources"]]
    for report in missing:
        paragraph = ResearchParagraph.model_json_schema()
        paragraph["properties"]["source_ids"]["items"]["enum"] = list(dict.fromkeys([*material_ids, *(sid for f in report["findings"] for sid in f["source_ids"])]))
        properties[report["doc_id"]] = paragraph
    schema = {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
    raw = generate(operation+":coverage_repair", w.BASE_SYSTEM+
        "\n你是研究统筹员。补齐综合报告遗漏的逐案比较。按每个给定doc_id返回一个对象（text、source_ids）。每段简明比较该案与本案的相似、差异和适用限制，约150字，必须引用该案findings中的至少一个source_id。本案事实只以material_sources为依据；无法比较的要素明确待核实。不得新增法条条号、事实或判决，不输出未提供的案件。", payload, state["run_id"], min(3200, 800*len(missing)), output_schema=schema)
    if set(raw) != set(properties):
        raise ValueError("综合研究仍遗漏了选定案例，未交付不完整结果")
    paragraphs = [ResearchParagraph.model_validate(raw[r["doc_id"]]).model_dump() for r in missing]
    for report, paragraph in zip(missing, paragraphs):
        case_ids = {sid for f in report["findings"] for sid in f["source_ids"]}
        if not case_ids.intersection(paragraph["source_ids"]):
            raise ValueError("补充对照遗漏了对应案例的来源")
    section = {"heading": "补充类案对照", "paragraphs": paragraphs}
    w.validate_paragraphs({"sections": [section]}, sources)
    result["sections"].append(section)


def synthesize(state, batch):
    from . import workflows as w
    if not batch["agents"] or any(a["status"] != "succeeded" for a in batch["agents"]):
        raise ValueError("必须收齐全部逐案研究后才能生成综合结果")
    w.guard(state, "全部逐案分析已收齐，正在综合研究")
    reports, sources = [], []
    for agent in batch["agents"]:
        item = next(i for i in batch["candidates"] if i["document_id"] == agent["doc_id"])
        reports.append({"doc_id": agent["doc_id"], "title": item["title"], "case_number": item.get("case_number", ""), "coverage": agent["coverage"], **agent["result"]})
        # Every cited original is sent with its report; no selected case is dropped.
        cited = {sid for f in agent["result"]["findings"] for sid in f["source_ids"]}
        sources.extend(s for s in agent["sources"] if s["id"] in cited)
    context = {"case": {k: state["snapshot"]["case"][k] for k in ("title", "position", "claims", "research_date")}, "goal": state["snapshot"]["goal"], "case_reports": reports}
    # Exact judgment excerpts can be long. The coordinator uses the specialists'
    # compact grounded reports; all referenced originals remain stored/resolvable.
    context["judgment_sources"] = [{"id": s["id"], "case_number": s.get("case_number", "")} for s in sources]
    materials_and_laws = w.research_context({**state, "selected": []})
    shown, omitted = [], 0
    for source in materials_and_laws:
        candidate = {**context, "sources": w.model_sources([*shown, source])}
        if len(dumps(candidate)) < 12000:
            shown.append(source)
        else:
            omitted += 1
    context["sources"] = w.model_sources(shown)
    context["omitted_source_count"] = omitted
    sources.extend(shown)
    system = w.BASE_SYSTEM + "\n你是研究统筹员，所有逐案报告已经收齐。结合本案材料和逐案报告给出有条件的研究结果。必须逐案比较每个选定案例的相似、不同、裁判理由及结果，逐案引用其source_ids，不能遗漏任何案例。公共判决不能证明客户案情。其他章节分析诉求的前置条件、可能结果及其条件、对方抗辩、不利事实、主位与备位方案和下一步。已付与未付款项、义务人与受领人必须分开；期间、送达和管辖未知则待核。不得新增法条、胜诉率或事实。法条只引用sources中的法律原文。返回3至6个sections，每节最多2段，每段尽量180字内，包含text与source_ids；另给unresolved。所有事实、理由及结果段落至少引用一个来源。"
    if len(system) + len(dumps(context)) > 14000:
        raise ValueError("全部逐案报告超过统筹上下文预算；未省略选定案件")
    with connect(True) as c:
        current = c.execute("SELECT status,synthesis_attempt FROM research_batches WHERE run_id=?", (state["run_id"],)).fetchone()
        cached = c.execute("SELECT 1 FROM model_calls WHERE operation_key=? AND status='succeeded'", (state["run_id"]+f":research_synthesis:{current['synthesis_attempt']}",)).fetchone()
        attempt = current["synthesis_attempt"] if current["status"] == "synthesizing" or cached else current["synthesis_attempt"] + 1
        if attempt > 2:
            raise ValueError("研究统筹已达两次尝试上限，请新建任务；逐案结果已保留")
        c.execute("UPDATE research_batches SET status='synthesizing',synthesis_attempt=?,updated_at=? WHERE run_id=?", (attempt, now(), state["run_id"]))
        event(c, state["run_id"], "research.synthesis.started", {"responses_received": batch["completed"], "expected": batch["total"]})
    schema = w.legal_schema(sources)
    schema["properties"]["sections"]["items"]["properties"]["paragraphs"]["items"]["properties"]["source_ids"]["minItems"] = 1
    result = generate(state["run_id"]+f":research_synthesis:{attempt}", system, context, state["run_id"], 3200, output_schema=schema)
    validate_report(state, result, sources, state["run_id"]+f":research_synthesis:{attempt}")
    complete_case_coverage(state, result, sources, reports, state["run_id"]+f":research_synthesis:{attempt}")
    cited = {sid for section in result["sections"] for p in section["paragraphs"] for sid in p["source_ids"]}
    covered = {s["doc_id"] for s in sources if s["id"] in cited and s["source_kind"] == "judgment"}
    if covered != set(batch["selected_doc_ids"]):
        raise ValueError("综合研究遗漏了选定案例，未交付不完整结果")
    result["unresolved"] = list(dict.fromkeys([*result.get("unresolved", []), "类案仅供比较研究，不能证明本案事实；结论及法源适用须结合原件复核。", *([f"本轮受上下文限制未纳入 {omitted} 条材料或法源摘录。"] if omitted else [])]))
    result["multi_agent"] = {"version": settings.RESEARCH_VERSION, "total": batch["total"], "completed": batch["completed"], "selected_doc_ids": batch["selected_doc_ids"], "case_reports": reports}
    result["conversation"] = []
    aid = w.save(state, "research", "类案研究与诉讼策略", report_body(result), result, sources)
    with connect(True) as c:
        c.execute("UPDATE research_batches SET status='succeeded',updated_at=? WHERE run_id=?", (now(), state["run_id"]))
        event(c, state["run_id"], "research.synthesis.completed", {"artifact_id": aid})
    return {"analysis": result, "sources": sources, "selected": batch["selected_doc_ids"], "outputs": [*state["outputs"], aid]}


def run_research(state):
    from . import workflows as w
    batch = prepare_batch(state)
    with connect() as c:
        old = c.execute("SELECT * FROM artifacts WHERE operation_key=?", (state["run_id"]+":research",)).fetchone()
        if old:
            sources = json.loads(old["sources"])
            w.check_sources(c, old["case_id"], sources)
            return {"analysis": json.loads(old["structured"]), "sources": sources, "selected": batch["selected_doc_ids"], "outputs": [*state["outputs"], old["id"]]}
    errors = []
    try:
        with ThreadPoolExecutor(max_workers=settings.EVIDENCE_CONCURRENCY, thread_name_prefix="research-case") as pool:
            pending = [pool.submit(analyze_one, state, did) for did in batch["selected_doc_ids"]]
            for future in as_completed(pending):
                try:
                    future.result()
                except Exception as exc:
                    errors.append(str(exc))
        with connect() as c:
            batch = batch_view(c, state["run_id"])
        if errors or batch["completed"] != batch["total"]:
            raise ValueError(f"逐案研究完成 {batch['completed']}/{batch['total']}，暂未汇总。成功结果已保存，可恢复未完成分析。"+"；".join(errors)[:300])
        return synthesize(state, batch)
    except Exception:
        with connect(True) as c:
            cancelled = c.execute("SELECT cancel_requested FROM runs WHERE id=?", (state["run_id"],)).fetchone()[0]
            c.execute("UPDATE research_batches SET status=?,updated_at=? WHERE run_id=?", ("cancelled" if cancelled else "failed", now(), state["run_id"]))
        raise
