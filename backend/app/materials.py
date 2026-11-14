import hashlib
import io
import json
import zipfile
from pathlib import Path
from fastapi import HTTPException
from . import settings
from .db import uid, now, dumps, digest, event

def parse_file(name, data):
    suffix = Path(name).suffix.lower()
    warnings, locations, parts = [], [], []
    def add(text, label):
        start = sum(len(p) for p in parts)
        parts.append(text + "\n")
        locations.append({"start": start, "end": start+len(text), "label": label})
    if suffix in (".txt", ".md"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("gb18030")
            except UnicodeDecodeError:
                raise HTTPException(422, "文本编码无法识别，请转换为 UTF-8。")
        if "\x00" in text:
            raise HTTPException(422, "文件不是可读文本。")
        for i, line in enumerate(text.splitlines()):
            add(line, f"第 {i+1} 行")
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if len(z.infolist()) > 2000 or sum(f.file_size for f in z.infolist()) > 100*1024*1024:
                    raise HTTPException(413, "DOCX 展开体积或条目数超过限制。")
                if "word/document.xml" not in z.namelist():
                    raise HTTPException(422, "不是有效的 DOCX。")
            from docx import Document
            d = Document(io.BytesIO(data))
            for i, p in enumerate(d.paragraphs):
                if p.text:
                    add(p.text, f"段落 {i+1}")
            for t, table in enumerate(d.tables):
                for r, row in enumerate(table.rows):
                    for k, cell in enumerate(row.cells):
                        if cell.text:
                            add(cell.text, f"表 {t+1} / 行 {r+1} / 列 {k+1}")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, "DOCX 解析失败，请另存为 DOCX 或粘贴正文。")
    elif suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise HTTPException(422, "文件内容不是 PDF。")
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise HTTPException(422, "加密 PDF 暂不支持，请上传解密后的文本型 PDF。")
            if len(reader.pages) > 100:
                raise HTTPException(413, "PDF 最多 100 页。")
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if not text.strip():
                    warnings.append(f"第 {i+1} 页未提取到文本；可能为扫描页或空白页。")
                add(text, f"第 {i+1} 页")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, "PDF 解析失败，请粘贴可读正文。")
    else:
        raise HTTPException(415, "支持 TXT、Markdown、DOCX、文本型 PDF。")
    text = "".join(parts)
    if len(text) > settings.MAX_CHARS:
        raise HTTPException(413, "提取文本超过 60,000 字，请拆分后上传；未截断入库。")
    status = "needs_transcription" if not text.strip() else "partial" if warnings else "ready"
    return text, locations, warnings, status

def store_material(c, case_id, name, text, kind, raw=None, locations=None, warnings=None, status="ready", logical_id=None, version=1):
    mid = uid("mat")
    raw = raw if raw is not None else text.encode()
    object_key = mid + ".bin"
    path = settings.DATA / "uploads" / object_key
    path.write_bytes(raw)
    path.chmod(0o600)
    c.execute("INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (mid, logical_id or mid, case_id, version, name, kind, text, digest(text), hashlib.sha256(raw).hexdigest(), object_key, dumps(locations or [{"start": 0, "end": len(text), "label": "录入正文"}]), status, dumps(warnings or []), 1, 0, now()))
    bump_case(c, case_id, "材料范围已变化，请新建任务使用最新材料。")
    return mid

def bump_case(c, case_id, reason, *, source_changed=True):
    c.execute("UPDATE cases SET revision=revision+1,updated_at=? WHERE id=?", (now(), case_id))
    if source_changed:
        c.execute("UPDATE cases SET source_revision=source_revision+1 WHERE id=?", (case_id,))
    runs = c.execute("SELECT id FROM runs WHERE case_id=? AND status IN ('queued','running','waiting_input')", (case_id,)).fetchall()
    for r in runs:
        c.execute("UPDATE runs SET cancel_requested=1,status='cancelled',stage='资料已变化',error=?,updated_at=? WHERE id=?", (reason, now(), r["id"]))
        event(c, r["id"], "run.cancelled", {"reason": reason})

def material_ref(material, quote):
    start = material["text"].find(quote)
    if start < 0 or not quote.strip():
        raise ValueError("模型引用未逐字匹配材料原文")
    locations = json.loads(material["locations"]) if isinstance(material["locations"], str) else material["locations"]
    return {"id": uid("ref"), "source_kind": "case_material", "doc_id": material["id"], "source_version": material["version"],
            "name": material["name"], "text_sha256": material["text_hash"], "start": start, "end": start+len(quote),
            "quote": quote, "location": next((l["label"] for l in locations if l["start"] <= start <= l["end"]), "录入正文")}

def material_document_ref(material):
    """A file association is not a quotation or a claim of evidentiary support."""
    return {"id": "M_" + material["id"], "source_kind": "case_material", "scope": "document",
            "doc_id": material["id"], "source_version": material["version"], "name": material["name"],
            "text_sha256": material["text_hash"], "file_sha256": material["file_hash"],
            "quote": "", "start": 0, "end": 0, "location": "整份材料（未指定引用片段）"}

def validate_material_ref(c, case_id, ref):
    row = c.execute("SELECT * FROM materials WHERE id=? AND case_id=? AND withdrawn=0", (ref["doc_id"], case_id)).fetchone()
    if not row or row["version"] != ref["source_version"] or row["text_hash"] != ref["text_sha256"]:
        return False
    if ref.get("scope") == "document":
        return (ref.get("quote") == "" and ref.get("start") == ref.get("end") == 0
                and ref.get("file_sha256") == row["file_hash"] and digest(row["text"]) == row["text_hash"])
    s, e = ref["start"], ref["end"]
    return 0 <= s < e <= len(row["text"]) and row["text"][s:e] == ref["quote"] and digest(row["text"]) == row["text_hash"]
