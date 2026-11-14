"""Template contracts, immutable draft versions and Word files in materials."""
import hashlib
import io
import json
import re
from pathlib import Path
from fastapi import HTTPException
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from . import settings
from .db import digest, dumps, now, uid, event
from .materials import material_document_ref
from .document_files import word_bytes

VERSION = "document-material-agents-v1"
TEMPLATES = {
    "complaint": {"name": "民事起诉状", "sections": ["当事人信息", "诉讼请求", "事实与理由", "证据目录", "受诉法院与落款"]},
    "opinion": {"name": "代理意见", "sections": ["代理立场与争议焦点", "事实与证据", "代理意见与依据", "抗辩与回应", "请求与结论"]},
    "client": {"name": "客户情况说明", "sections": ["已有信息", "当前进展", "待核事项", "下一步安排"]},
    "internal": {"name": "内部研究报告", "sections": ["目标与范围", "事实与证据", "争议与分析", "处理路径", "后续事项"]},
}


def builtin_templates():
    return [{"id": "builtin:"+kind, "document_type": kind, **value,
             "text": "# "+value["name"]+"\n\n"+"\n\n".join("## "+s+"\n【按本案材料填写；缺失信息写待补充】" for s in value["sections"])}
            for kind, value in TEMPLATES.items()]


def template_snapshot(c, cid, template_id, kind):
    template_id = template_id or "builtin:"+kind
    if template_id.startswith("builtin:"):
        template = next((t for t in builtin_templates() if t["id"] == template_id), None)
        if not template or template["document_type"] != kind:
            raise HTTPException(422, "请选择与文书类型对应的模板。")
        return {**template, "hash": digest(template)}
    row = c.execute("SELECT * FROM materials WHERE id=? AND case_id=? AND active=1 AND withdrawn=0", (template_id, cid)).fetchone()
    if not row or row["status"] != "ready":
        raise HTTPException(422, "请选择本案完整可读的当前模板。")
    if len(row["text"]) > 7000:
        raise HTTPException(413, "模板最多 7,000 字，请提供仅含结构与填写要求的模板。")
    # Word heading styles, Markdown headings and named placeholders are stable
    # structural identifiers. No template example is ever treated as evidence.
    headings = re.findall(r"(?m)^#{2,3}\s+(.+?)\s*$", row["text"])
    text = row["text"]
    if Path(row["name"]).suffix.lower() == ".docx":
        doc = Document(io.BytesIO((settings.DATA/"uploads"/row["object_key"]).read_bytes()))
        # Include table cells in their actual position in the template.
        text = "\n".join(Paragraph(p, doc).text for p in doc.element.body.iter(qn("w:p")))
        headings = [p.text.strip() for p in doc.paragraphs if p.text.strip() and p.style and p.style.name.startswith("Heading")] or headings
    slots = list(dict.fromkeys(re.findall(r"\{\{\s*([^{}\n]{1,80}?)\s*\}\}", text)))
    headings = slots or headings or [line.strip() for line in text.splitlines()
        if re.match(r"^[一二三四五六七八九十]+[、．.]", line.strip()) and len(line.strip()) <= 80]
    headings = list(dict.fromkeys(headings)) or ["正文"]
    if len(headings) > 12 or any(len(h) > 100 for h in headings):
        raise HTTPException(422, "模板最多 12 个章节，每个标题不超过 100 字。")
    ref = {**material_document_ref(row), "role": "template"}
    return {"id": row["id"], "name": row["name"], "text": text, "sections": headings,
            "slots": slots, "hash": row["text_hash"], "source": ref, "document_type": kind}


def draft_context(c, cid, req, artifact):
    if artifact["case_id"] != cid or artifact["kind"] != "document":
        raise HTTPException(422, "只能继续修改本案的文书。")
    if artifact["stale"] or not artifact["sources"]:
        raise HTTPException(409, "文书资料已变化，请按当前材料重新起草。")
    if req.document_content_hash != artifact["content_hash"] or c.execute("SELECT 1 FROM artifacts WHERE parent_id=?", (artifact["id"],)).fetchone():
        raise HTTPException(409, "文书已有新版本，请重新加载后修改。")
    if c.execute("SELECT 1 FROM runs WHERE case_id=? AND status IN ('queued','running') AND json_extract(snapshot,'$.document_context.id')=?", (cid, artifact["id"])).fetchone():
        raise HTTPException(409, "当前版本的修改尚未完成，请等待后继续。")
    return {key: artifact[key] for key in ("id", "version", "content_hash", "body", "structured")}



def archive_draft(c, aid):
    row = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    a = dict(row)
    for key in ("structured", "sources", "checks"): a[key] = json.loads(a[key])
    if not a["checks"].get("passed"): return None
    existing = c.execute("SELECT id FROM reference_documents WHERE artifact_id=? AND deleted_at IS NULL", (aid,)).fetchone()
    if existing: return existing["id"]
    content = word_bytes(c, a)
    run = c.execute("SELECT user_id FROM runs WHERE id=?", (a["run_id"],)).fetchone()
    did = uid("refdoc")
    name = re.sub(r'[\\/:*?"<>|]', '_', a["title"]) + f"_v{a['version']}_{aid[-8:]}.docx"
    c.execute("INSERT INTO reference_documents(id,case_id,artifact_id,answer_ids,name,content,content_hash,user_id,created_at,idempotency_key,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (did, a["case_id"], aid, "[]", name, content, hashlib.sha256(content).hexdigest(), run["user_id"], now(), "draft:"+aid, digest({"artifact": aid, "hash": a["content_hash"]})))
    event(c, a["run_id"], "document.archived", {"artifact_id": aid, "document_id": did})
    return did
