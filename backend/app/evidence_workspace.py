"""Evidence selection, presentation and grounded follow-up shared by UI and Word."""
import re
import json
from copy import deepcopy
from fastapi import HTTPException
from pydantic import Field
from .contracts import Strict
from . import settings
from .db import connect, dumps
from .similar_cases import similar_state
from .evidence_questions import bounded_questions, question_details


def distinct_cases(items):
    result, seen = [], set()
    for item in items:
        number = re.sub(r"\s+", "", item.get("case_number", "")).replace("（", "(").replace("）", ")")
        identity = number or item["document_id"]
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def selection_snapshot(c, case, selection=None, *, max_cases=5, purpose="证据分析"):
    saved = similar_state(c, case)
    if selection and selection.similar_version != saved["version"]:
        raise HTTPException(409, "类案列表已更新，请刷新并重新选择。")
    if saved["response"] is None or saved["needs_analysis"]:
        raise HTTPException(409, f"请先在“类案”页按当前条件完成分析，再选择{purpose}范围。")
    candidates = distinct_cases(saved["response"]["items"])
    ids = selection.doc_ids if selection else [i["document_id"] for i in candidates[:settings.DEFAULT_CASE_ANALYSIS_COUNT]]
    if not ids:
        raise HTTPException(422, "当前筛选结果没有可分析类案，请返回“类案”页调整条件。")
    if len(ids) > max_cases or len(ids) != len(set(ids)) or not set(ids).issubset({i["document_id"] for i in candidates}):
        raise HTTPException(422, f"只能选择当前“类案”列表中保留的 1 至 {max_cases} 个不同案件。")
    return {"doc_ids": ids, "candidates": candidates, "similar_version": saved["version"],
            "analyzed_at": saved["analyzed_at"], "source_revision": saved["analyzed_source_revision"],
            "retrieval": {k: v for k, v in saved["response"].items() if k != "items"},
            "options": saved["analyzed_options"], "selection_mode": "filtered_selected" if selection else "filtered_default"}


def evidence_view(artifact):
    """Normalize old reports without rewriting their saved analysis or hashes."""
    data = artifact["structured"]
    if not data:
        return {"existing_evidence": [], "additional_evidence": [], "follow_up_questions": [], "follow_up_details": [], "notices": []}
    existing = data.get("existing_evidence")
    if existing is None:
        grouped = {}
        for ref in artifact["sources"]:
            if ref["source_kind"] != "case_material":
                continue
            item = grouped.setdefault(ref["doc_id"], {"material_id": ref["doc_id"], "name": ref["name"],
                "status": "已提供材料 · 原件及证明力待核验", "source_ids": [], "excerpts": [], "issues": []})
            item["source_ids"].append(ref["id"])
            item["excerpts"].append({"source_id": ref["id"], "quote": ref.get("quote", ""), "location": ref.get("location", "")})
        for item in grouped.values():
            item["issues"] = list(dict.fromkeys(i["issue"] for i in data.get("items", [])
                if set(i.get("material_source_ids", [])) & set(item["source_ids"])))
        existing = list(grouped.values())
    additional = [{"issue": i["issue"], "evidence_needed": i.get("evidence_needed") or i.get("next_step", "待核实材料范围"),
        "why_needed": i.get("why_needed") or "用于核实“"+i["issue"]+"”，并与已有材料及原件相互核对。",
        "obtain_from": i.get("obtain_from", ""), "next_step": i.get("next_step", ""),
        "material_source_ids": i.get("material_source_ids", []), "source_ids": i.get("source_ids", [])}
        for i in data.get("items", [])]
    questions = data.get("follow_up_details", data.get("follow_up_questions"))
    if questions is None:
        questions = [i.get("verification_question", "") for i in data.get("items", [])]
        questions += [q for q in data.get("unresolved", []) if q.rstrip().endswith(("？", "?"))]
    details = question_details(questions, data.get("items", []))
    notices = data.get("notices", [q for q in data.get("unresolved", []) if not q.rstrip().endswith(("？", "?"))])
    return {"existing_evidence": existing, "additional_evidence": additional,
            "follow_up_questions": [item["question"] for item in details], "follow_up_details": details, "notices": notices}


class AnswerParagraph(Strict):
    text: str = Field(min_length=1, max_length=300)
    source_ids: list[str] = Field(min_length=1, max_length=6)


class EvidenceAnswer(Strict):
    paragraphs: list[AnswerParagraph] = Field(min_length=1, max_length=3)
    limitations: list[str] = Field(min_length=1, max_length=3)


def followup(state):
    if "evidence.followup" not in state["plan"]:
        return {}
    from . import workflows as w
    from .model import generate
    context = state["snapshot"]["evidence_context"]
    w.guard(state, "结合本轮证据分析回答追问")
    def check(c):
        parent = c.execute("SELECT checks,content_hash,case_revision FROM artifacts WHERE id=? AND case_id=?", (context["id"], state["snapshot"]["case"]["id"])).fetchone()
        case = c.execute("SELECT revision FROM cases WHERE id=?", (state["snapshot"]["case"]["id"],)).fetchone()
        if not parent or parent["content_hash"] != context["content_hash"] or not json.loads(parent["checks"]).get("passed") or parent["case_revision"] != case["revision"]:
            raise ValueError("引用的证据分析已变化或停止使用，请先更新分析。")
        w.check_sources(c, state["snapshot"]["case"]["id"], context["sources"])
    with connect() as c:
        check(c)
    # Select exact excerpts within the context budget, prioritizing question terms.
    question = state["snapshot"]["goal"]
    terms = set(re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9_]+", question))
    terms |= {question[i:i+2] for i in range(len(question)-1) if re.match(r"[\u3400-\u9fff]{2}", question[i:i+2])}
    include_judgments = bool(re.search(r"类案|判例|判决|裁判|法院|采信", question))
    eligible = [s for s in context["sources"] if s["source_kind"] == "case_material" or include_judgments]
    sources = sorted(eligible, key=lambda s: (s["source_kind"] == "case_material", sum(t in s.get("quote", "") for t in terms)), reverse=True)
    shown, size = [], 0
    for source in sources:
        cost = len(source.get("quote", "")) + 150
        if size + cost <= 6500:
            shown.append(source); size += cost
    if not shown:
        raise ValueError("没有可用于回答的证据来源，请更新证据分析。")
    system = w.BASE_SYSTEM + "\n你是证据核验助手，只回答当前问题，不遍历整份分析报告。回答1至3个简短段落，每段尽量100字以内。只说明证据用途、需要核对的内容、补证渠道。不要复述案情、身份、金额或日期。不得输出违约成立、解除权成立、应赔偿、某证照为必需等实体法律结论。不得以未见某条摘录断言合同未约定、证据不存在或材料未提供。必须区分材料摘要和已经取得的原件。公共判决仅为类案线索，使用时明确说‘类案原文’，不得把其中的事实用于本案。用户新陈述未经核实，历史回答也可能有误；事实依据只取本次sources。超出证据问题时说明需另行研究。返回paragraphs（text、source_ids）和limitations；每段至少引用一个给定来源ID，不新增法条。"
    # Material questions should not inherit unrelated legal conclusions from the
    # full report. Keep only the most relevant evidence tasks as research context.
    tasks = context.get("evidence_tasks", [])
    relevant = sorted(tasks, key=lambda i: sum(t in dumps(i) for t in terms), reverse=True)[:2]
    summary = dumps(relevant) if relevant else "请围绕当前来源说明证据核对的方法及范围。"
    payload = {"question": question, "evidence_summary": summary[:2000],
               "conversation": list(context["conversation"]), "sources": w.model_sources(shown)}
    while len(system)+len(dumps(payload)) > 14000 and payload["conversation"]:
        payload["conversation"].pop(0)
    while len(system)+len(dumps(payload)) > 14000 and len(shown) > 1:
        shown.pop()
        payload["sources"] = w.model_sources(shown)
    schema = deepcopy(EvidenceAnswer.model_json_schema())
    schema["$defs"]["AnswerParagraph"]["properties"]["source_ids"]["items"]["enum"] = [s["id"] for s in shown]
    raw = generate(state["run_id"]+":evidence_followup", system, payload, state["run_id"], 1800, output_schema=schema)
    result = EvidenceAnswer.model_validate(raw).model_dump()
    w.validate_paragraphs({"sections": [{"heading": "证据追问", "paragraphs": result["paragraphs"]}]}, shown)
    if len(shown) < len(sources):
        result["limitations"].append(f"本次回答使用 {len(shown)} 条相关原文摘录；未覆盖全部来源，仍需结合原件核对。")
    result.update(evidence_artifact_id=context["id"], question=question)
    body = "# 证据追问\n\n" + question + "\n\n" + "\n\n".join(p["text"]+" "+" ".join("["+sid+"]" for sid in p["source_ids"]) for p in result["paragraphs"])
    body += "\n\n"+"\n\n".join(result["limitations"])
    with connect() as c:
        check(c)
    aid = w.save(state, "evidence_answer", "证据追问", body, result, shown)
    return {"outputs": [*state["outputs"], aid]}
