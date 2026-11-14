"""Grounded chat revisions of a specific research version."""
from copy import deepcopy
import json
from .db import connect, dumps, digest, uid, now, event
from .model import generate
from .research_agents import report_body, validate_report


def bounded_sources(sources, payload):
    """Keep client facts and law before filling remaining space with judgments."""
    from . import workflows as w
    primary = [s for s in sources if s["source_kind"] in ("case_material", "law")]
    first, remainder, seen = [], [], set()
    for source in sources:
        if source["source_kind"] != "judgment":
            continue
        if source["doc_id"] not in seen:
            seen.add(source["doc_id"])
            first.append(source)
        else:
            remainder.append(source)
    shown = []
    for source in [*primary, *first, *remainder]:
        if len(dumps({**payload, "sources": w.model_sources([*shown, source])})) < 12500:
            shown.append(source)
    return shown


def followup(state):
    if "research.followup" not in state["plan"]:
        return {}
    from . import workflows as w
    context = state["snapshot"]["research_context"]
    w.guard(state, "结合本轮研究回答追问并完善报告")
    def check(c):
        parent = c.execute("SELECT * FROM artifacts WHERE id=?", (context["id"],)).fetchone()
        case = c.execute("SELECT revision FROM cases WHERE id=?", (state["snapshot"]["case"]["id"],)).fetchone()
        if not parent or parent["content_hash"] != context["content_hash"] or not json.loads(parent["checks"]).get("passed") or parent["case_revision"] != case["revision"]:
            raise ValueError("引用的研究版本已变化或停止使用，请更新研究后再追问")
        w.check_sources(c, parent["case_id"], context["sources"])
    with connect() as c:
        check(c)
    payload = {"question": state["snapshot"]["goal"], "current_report": context["structured"]["sections"],
               "unresolved": context["structured"].get("unresolved", []),
               "conversation": [{"question": t["question"][:600], "answer": t["answer"][:600]} for t in context["structured"].get("conversation", [])[-3:]]}
    shown = bounded_sources(context["sources"], payload)
    if not shown:
        raise ValueError("当前报告超过追问上下文预算，请缩小研究范围后重试")
    payload["sources"] = w.model_sources(shown)
    payload["omitted_source_count"] = len(context["sources"]) - len(shown)
    system = w.BASE_SYSTEM + "\n你是研究报告编辑。回答用户追问，并返回完善后的完整研究报告，保留仍有依据的结论、限制及不利事项。用户的新陈述仅为待核实信息，不得当作已证事实。既有报告和聊天是上下文，事实及法律结论仅以本次sources为依据。超出来源范围的问题明确说明无法确定和应核实的材料。返回answer（简短回应），sections（完整更新报告，每节paragraphs含text及source_ids），unresolved。保持3至6节，总计约1200字；每段引用给定来源ID。本轮正文不要写法条条号，直接以来源ID引用对应法源，不能编造或猜测ID。围绕追问写具体行动与条件，不重述无关类案事实，不沿用旧稿中无法由本次原文支持的解释。不能把当前材料未覆盖的信息断言为不存在，不能新增未经来源支持的案号、金额或确定事实。"
    schema = deepcopy(w.legal_schema(shown))
    schema["properties"]["answer"] = {"type": "string", "minLength": 1, "maxLength": 600}
    schema["required"].append("answer")
    raw = generate(state["run_id"]+":research_followup", system, payload, state["run_id"], 3200, output_schema=schema)
    validate_report(state, raw, shown, state["run_id"]+":research_followup")
    answer = raw.get("answer")
    if not isinstance(answer, str) or not 1 <= len(answer) <= 600:
        raise ValueError("追问回应结构不完整")
    # The brief answer is checked against the same sources as the revised report.
    w.validate_paragraphs({"sections": [{"heading": "研究追问", "paragraphs": [{"text": answer, "source_ids": [s["id"] for s in shown]}]}]}, shown)
    result = {"sections": raw["sections"], "unresolved": raw.get("unresolved", []),
              "multi_agent": context["structured"].get("multi_agent"),
              "conversation": [*context["structured"].get("conversation", []), {"question": payload["question"], "answer": answer}],
              "refined_from": context["id"]}
    if payload["omitted_source_count"]:
        result["unresolved"].append(f"本次完善未纳入 {payload['omitted_source_count']} 条原始摘录，应结合完整来源核对。")
    # Validate the parent and save the new version in the same transaction, so
    # withdrawal/rejection cannot leave a successful-looking orphan revision.
    w.guard(state, "保存完善后的研究报告")
    with connect(True) as c:
        check(c)
        operation = state["run_id"]+":research"
        old = c.execute("SELECT id FROM artifacts WHERE operation_key=?", (operation,)).fetchone()
        if old:
            aid = old["id"]
        else:
            aid = uid("art")
            body = report_body(result)
            checks = {"passed": True, "exact_sources": len(context["sources"]), "semantic_review": "pending"}
            c.execute("INSERT INTO artifacts(id,case_id,run_id,kind,version,parent_id,title,body,structured,sources,content_hash,checks,completeness,case_revision,created_at,operation_key) VALUES (?,?,?,'research',?,?,?,?,?,?,?,?,'limited',?,?,?)",
                (aid, state["snapshot"]["case"]["id"], state["run_id"], context["version"]+1, context["id"], "类案研究与诉讼策略", body, dumps(result), dumps(context["sources"]), digest(body), dumps(checks), state["snapshot"]["case"]["revision"], now(), operation))
            event(c, state["run_id"], "artifact.created", {"artifact_id": aid, "kind": "research", "parent_id": context["id"]})
    return {"outputs": [*state["outputs"], aid]}
