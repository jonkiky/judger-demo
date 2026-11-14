"""Case-scoped search preferences and the last successful, immutable response."""
import json
import re
from fastapi import HTTPException
from .contracts import SimilarCaseOptions
from .db import dumps, now


def normalized_options(options):
    result = options.model_dump(mode="json") if isinstance(options, SimilarCaseOptions) else dict(options)
    result["filters"] = {key: sorted(values) for key, values in result["filters"].items() if values}
    return result


def record_for(c, cid):
    return c.execute("SELECT * FROM similar_cases WHERE case_id=?", (cid,)).fetchone()


def exclusions_for(c, cid):
    return c.execute("SELECT * FROM similar_case_exclusions WHERE case_id=? ORDER BY created_at DESC,doc_id", (cid,)).fetchall()


def visible_response(row, excluded):
    response = json.loads(row["response"]) if row and row["response"] else None
    if response is not None:
        response["items"] = [item for item in response["items"] if item["document_id"] not in excluded]
    return response


def summary(row, case, excluded=()):
    response = visible_response(row, excluded)
    stale = response is not None and row["source_revision"] != case["source_revision"]
    options_changed = response is not None and row["options"] != row["analyzed_options"]
    return {"analyzed_at": row["analyzed_at"] if row else None, "count": len(response["items"]) if response else 0, "excluded_count": len(excluded),
            "stale": stale, "options_changed": options_changed, "needs_analysis": stale or options_changed,
            "status": "stale" if stale or options_changed else "ready" if response is not None else "not_analyzed"}


def similar_summary(c, case):
    return summary(record_for(c, case["id"]), case, {r["doc_id"] for r in exclusions_for(c, case["id"])})


def suggested_query(c, case):
    materials = c.execute("SELECT name,text,status FROM materials WHERE case_id=? AND active=1 AND withdrawn=0 AND kind!='文书模板' ORDER BY created_at,id", (case["id"],)).fetchall()
    compact = lambda text: re.sub(r"\s+", " ", text).strip()
    overview = f"案件：{compact(case['title'])[:160]}\n代理立场：{compact(case['position'])[:100]}\n诉求与争议：{compact(case['claims'])[:600]}"
    readable = [m for m in materials if m["text"].strip()]
    warnings = []
    if any(m["status"] != "ready" for m in materials):
        warnings.append("部分材料尚未完整解析；本次只能使用案件概览和已读取的材料文字。")
    if len(compact(case["claims"])) > 600:
        warnings.append("研究目标较长，自动检索描述使用前 600 字；可编辑描述补充重点。")
    chosen = readable[:20]
    parts = [overview]
    if chosen:
        budget = max(0, 1500 - len(overview) - len(chosen)) // len(chosen)
        for material in chosen:
            excerpt = f"{compact(material['name'])[:20]}：{compact(material['text'])}"
            parts.append(excerpt[:budget])
        warnings.append("自动检索描述使用材料开头的限长摘录（合计最多 1,500 字）；可编辑描述补充关键事实或争点。")
    if len(readable) > 20:
        warnings.append("材料超过 20 份，自动检索描述使用前 20 份可读材料。")
    return "\n".join(parts), warnings, len(materials), len(readable)


def similar_state(c, case):
    row = record_for(c, case["id"])
    exclusions = exclusions_for(c, case["id"])
    excluded = {r["doc_id"] for r in exclusions}
    original = json.loads(row["response"]) if row and row["response"] else None
    original_ids = {item["document_id"] for item in original["items"]} if original else set()
    query, warnings, count, readable = suggested_query(c, case)
    return {**summary(row, case, excluded), "version": row["version"] if row else 0,
            "options": json.loads(row["options"]) if row else normalized_options(SimilarCaseOptions()),
            "response": visible_response(row, excluded),
            "excluded_items": [{"item": json.loads(r["item"]), "reason": r["reason"], "excluded_at": r["created_at"],
                                "in_current_results": r["doc_id"] in original_ids} for r in exclusions],
            "excluded_from_current_count": len(original_ids & excluded),
            "analyzed_options": json.loads(row["analyzed_options"]) if row and row["analyzed_options"] else None,
            "analyzed_source_revision": row["source_revision"] if row else None,
            "source_revision": case["source_revision"], "updated_at": row["updated_at"] if row else None,
            "suggested_query": query, "query_warnings": warnings,
            "material_count": count, "readable_material_count": readable}


def set_relevance(c, cid, doc_id, relevant, user_id):
    row = record_for(c, cid)
    response = json.loads(row["response"]) if row and row["response"] else None
    item = next((item for item in response["items"] if item["document_id"] == doc_id), None) if response else None
    excluded = c.execute("SELECT 1 FROM similar_case_exclusions WHERE case_id=? AND doc_id=?", (cid, doc_id)).fetchone()
    if item is None and excluded is None:
        raise HTTPException(404, "该文书不在本案已保存的类案中，请刷新后重试。")
    if relevant and excluded:
        c.execute("DELETE FROM similar_case_exclusions WHERE case_id=? AND doc_id=?", (cid, doc_id))
    elif not relevant and not excluded:
        c.execute("INSERT INTO similar_case_exclusions(case_id,doc_id,item,user_id,created_at) VALUES (?,?,?,?,?)",
                  (cid, doc_id, dumps(item), user_id, now()))
    else:
        return
    c.execute("UPDATE similar_cases SET version=version+1,updated_at=? WHERE case_id=?", (now(), cid))


def check_version(c, cid, expected):
    row = record_for(c, cid)
    if (row["version"] if row else 0) != expected:
        raise HTTPException(409, "类案条件或结果已在其他页面更新，请载入已保存条件后重试。")


def save_similar(c, cid, req, *, response=None, source_revision=None):
    options = dumps(normalized_options(req.options))
    stamp = now()
    if response is None:
        c.execute("""INSERT INTO similar_cases(case_id,version,options,updated_at) VALUES (?,?,?,?)
                     ON CONFLICT(case_id) DO UPDATE SET version=excluded.version,options=excluded.options,updated_at=excluded.updated_at""",
                  (cid, req.version+1, options, stamp))
    else:
        c.execute("""INSERT INTO similar_cases(case_id,version,options,response,analyzed_options,source_revision,analyzed_at,updated_at)
                     VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET version=excluded.version,options=excluded.options,
                     response=excluded.response,analyzed_options=excluded.analyzed_options,source_revision=excluded.source_revision,
                     analyzed_at=excluded.analyzed_at,updated_at=excluded.updated_at""",
                  (cid, req.version+1, options, dumps(response), options, source_revision, stamp, stamp))
