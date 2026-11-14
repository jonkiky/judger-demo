import asyncio
import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from fastapi import FastAPI, Request, Response, Depends, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from . import settings
from .db import init, connect, uid, now, dumps, digest, password_hash, audit, event
from .contracts import Login, CaseInput, CaseSummary, CaseDeletion, TextMaterial, Goal, SearchInput, FacetInput, Selection, Resume, EditArtifact, Review, WritingConfig, PublishConfig, Feedback, SimilarCaseInput, SimilarCaseRelevance
from .materials import store_material, parse_file, bump_case
from .document_workspace import builtin_templates, template_snapshot, draft_context, archive_draft, VERSION as DOCUMENT_VERSION
from .contracts import EditTimeline, EvidenceReference
from .timelines import edited_timeline
from .model import service
from .workflows import route, check_sources, law_sources
from .evidence_agents import batch_view
from .research_agents import batch_view as research_batch_view, validate_selection as validate_research_selection
from .evidence_workspace import evidence_view, selection_snapshot
from .similar_cases import similar_state, similar_summary, check_version, save_similar, set_relevance

@asynccontextmanager
async def lifespan(app):
    init()
    yield

app = FastAPI(title="裁研案件研究 POC", version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

@app.middleware("http")
async def same_origin(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return Response(dumps({"detail": "跨来源写操作被拒绝"}), 403, media_type="application/json")
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    return response

def user(request: Request):
    token = request.cookies.get("caiyan_session", "")
    with connect() as c:
        u = c.execute("SELECT u.id,u.username,u.name,u.role,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.disabled=0", (digest(token), time.time())).fetchone()
    if not u:
        raise HTTPException(401, "请登录本机工作区")
    if request.method not in ("GET", "HEAD") and not secrets.compare_digest(request.headers.get("x-csrf-token", ""), u["csrf"]):
        raise HTTPException(403, "会话校验失败，请刷新后重试")
    return dict(u)

def admin(u=Depends(user)):
    if u["role"] != "admin":
        raise HTTPException(403, "需要配置管理员权限")
    return u

def case_for(c, cid, u):
    row = c.execute("SELECT * FROM cases WHERE id=? AND deleted_at IS NULL AND (owner_id=? OR id IN (SELECT case_id FROM case_members WHERE user_id=?))", (cid, u["id"], u["id"])).fetchone()
    if not row:
        raise HTTPException(404, "案件不存在或无权访问")
    return dict(row)

def run_for(c, rid, u):
    row = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    case_for(c, row["case_id"], u)
    return dict(row)

def artifact_for(c, aid, u):
    row = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    if not row:
        raise HTTPException(404, "成果不存在")
    case = case_for(c, row["case_id"], u)
    a = dict(row)
    for key in ("sources", "checks", "structured"):
        a[key] = json.loads(a[key])
    a["stale"] = a["case_revision"] != case["revision"]
    withdrawn = any(s["source_kind"] == "case_material" and not c.execute("SELECT 1 FROM materials WHERE id=? AND case_id=? AND withdrawn=0", (s["doc_id"], a["case_id"])).fetchone() for s in a["sources"])
    if a["kind"] == "evidence_answer":
        parent = c.execute("SELECT checks,sources FROM artifacts WHERE id=? AND case_id=?", (a["structured"].get("evidence_artifact_id"), a["case_id"])).fetchone()
        parent_withdrawn = parent and any(s["source_kind"] == "case_material" and not c.execute("SELECT 1 FROM materials WHERE id=? AND case_id=? AND withdrawn=0", (s["doc_id"], a["case_id"])).fetchone() for s in json.loads(parent["sources"]))
        if not parent or not json.loads(parent["checks"]).get("passed") or parent_withdrawn:
            withdrawn = True
    if withdrawn:
        a["body"] = "来源已撤回。此成果已停止展示和导出。"
        a["structured"] = {}
        a["sources"] = []
        a["checks"] = {"passed": False, "issues": ["来源已撤回"]}
        a["stale"] = True
    a["review_status"] = "reviewed" if a["reviewed_by"] and not a["stale"] and a["checks"]["passed"] else "pending"
    if a["kind"] == "evidence":
        a["evidence_view"] = evidence_view(a)
    return a

def material_summary(m):
    d = dict(m)
    d["characters"] = len(d.pop("text", ""))
    d.pop("object_key", None)
    d["warnings"] = json.loads(d["warnings"])
    d["locations"] = json.loads(d["locations"])
    return d

login_attempts = {}
@app.post("/api/v1/auth/login")
def login(req: Login, response: Response, request: Request):
    ip = request.client.host
    times = [t for t in login_attempts.get(ip, []) if t > time.time()-60]
    if len(times) >= 12:
        raise HTTPException(429, "登录尝试过多，请一分钟后重试")
    login_attempts[ip] = [*times, time.time()]
    with connect(True) as c:
        u = c.execute("SELECT * FROM users WHERE username=? AND disabled=0", (req.username,)).fetchone()
        if not u or not secrets.compare_digest(password_hash(req.password, u["salt"]), u["password_hash"]):
            raise HTTPException(401, "账号或密码不正确")
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(24)
        c.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest(token), u["id"], csrf, time.time()+8*3600))
        audit(c, u["id"], "login", u["id"])
    response.set_cookie("caiyan_session", token, httponly=True, samesite="lax", max_age=8*3600, path="/")
    return {"id": u["id"], "name": u["name"], "role": u["role"], "username": u["username"], "csrf": csrf}

@app.get("/api/v1/me")
def me(u=Depends(user)):
    return u

@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response, u=Depends(user)):
    with connect(True) as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies.get("caiyan_session", "")),))
    response.delete_cookie("caiyan_session", path="/")
    return {"ok": True}

@app.get("/api/v1/health")
def health(u=Depends(user)):
    release_path = settings.CORPUS/"release.json"
    release = json.loads(release_path.read_text()) if release_path.exists() else None
    results = {}
    for name, base in [("search", settings.SEARCH_URL), ("gateway", settings.GATEWAY)]:
        try:
            results[name] = {"status": "ready", "details": service("GET", "/health", base=base, timeout=5)}
        except Exception as exc:
            results[name] = {"status": "unavailable", "reason": str(exc)[:150]}
    return {"mode": "local_poc", "corpus": release, "services": results, "workflow_version": settings.WORKFLOW_VERSION}

@app.get("/api/v1/cases", response_model=list[CaseSummary])
def cases(u=Depends(user)):
    with connect() as c:
        rows = c.execute("""
            SELECT c.id,c.owner_id,c.title,c.position,c.claims,c.research_date,
                c.revision,c.source_revision,c.created_at,c.updated_at,
                (SELECT count(*) FROM materials WHERE case_id=c.id AND active=1 AND withdrawn=0) material_count,
                (SELECT count(*) FROM runs WHERE case_id=c.id AND status='waiting_input') waiting_count,
                COALESCE(
                    (SELECT status FROM runs WHERE case_id=c.id AND revision=c.revision
                     ORDER BY CASE status WHEN 'waiting_input' THEN 0 WHEN 'running' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,
                              created_at DESC, id DESC LIMIT 1),
                    CASE WHEN EXISTS(SELECT 1 FROM materials WHERE case_id=c.id AND active=1 AND withdrawn=0)
                         THEN 'ready_to_research' ELSE 'needs_materials' END
                ) work_status
            FROM cases c
            WHERE deleted_at IS NULL AND (owner_id=? OR id IN (SELECT case_id FROM case_members WHERE user_id=?))
            ORDER BY updated_at DESC, id
        """, (u["id"], u["id"])).fetchall()
        return [{**dict(r), "similar_cases": similar_summary(c, dict(r))} for r in rows]

@app.post("/api/v1/cases", status_code=201)
def create_case(req: CaseInput, u=Depends(user)):
    cid = uid("case")
    with connect(True) as c:
        c.execute("INSERT INTO cases (id,owner_id,title,position,claims,research_date,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (cid, u["id"], req.title, req.position, req.claims, req.research_date.isoformat(), 1, now(), now()))
        audit(c, u["id"], "case.create", cid)
        return case_for(c, cid, u)

@app.delete("/api/v1/cases/{cid}", response_model=CaseDeletion, responses={
    403: {"description": "Only the case owner may delete; valid CSRF is required."},
    404: {"description": "Case is deleted, missing, or inaccessible."},
    409: {"description": "Case revision changed; reload before confirming deletion."},
})
def delete_case(cid: str, if_match: int = Header(), u=Depends(user)):
    """Remove a case from the workspace and cancel active work, retaining local history."""
    with connect(True) as c:
        case = case_for(c, cid, u)
        if case["owner_id"] != u["id"]:
            raise HTTPException(403, "仅案件创建者可以删除案件")
        if case["revision"] != if_match:
            raise HTTPException(409, "案件已更新，请关闭确认区域并刷新列表后重新确认删除")
        bump_case(c, cid, "案件已删除", source_changed=False)
        c.execute("UPDATE cases SET deleted_at=? WHERE id=?", (now(), cid))
        audit(c, u["id"], "case.delete", cid)
    return {"id": cid, "status": "deleted"}

@app.get("/api/v1/cases/{cid}")
def get_case(cid: str, u=Depends(user)):
    with connect() as c:
        case = case_for(c, cid, u)
        case["materials"] = [material_summary(m) for m in c.execute("SELECT * FROM materials WHERE case_id=? AND active=1 AND withdrawn=0 ORDER BY created_at", (cid,))]
        case["runs"] = [{**dict(r), "plan": json.loads(r["plan"]), "wait_payload": json.loads(r["wait_payload"]) if r["wait_payload"] else None} for r in c.execute("SELECT id,goal,status,stage,error,plan,wait_payload,created_at,updated_at,json_extract(snapshot,'$.evidence_context.id') evidence_artifact_id,json_extract(snapshot,'$.research_context.id') research_artifact_id FROM runs WHERE case_id=? ORDER BY created_at DESC", (cid,))]
        case["artifacts"] = [artifact_for(c, a[0], u) for a in c.execute("SELECT id FROM artifacts WHERE case_id=? ORDER BY created_at DESC", (cid,)).fetchall()]
        case["selected_doc_ids"] = [r[0] for r in c.execute("SELECT doc_id FROM selections WHERE case_id=?", (cid,))]
        case["similar_cases"] = similar_summary(c, case)
        case["reference_documents"] = reference_list(c, cid, u)
        return case

@app.patch("/api/v1/cases/{cid}")
def update_case(cid: str, req: CaseInput, if_match: int = Header(), u=Depends(user)):
    with connect(True) as c:
        case = case_for(c, cid, u)
        if case["revision"] != if_match:
            raise HTTPException(409, "案件已更新，请刷新后重新编辑")
        if any(case[key] != value for key, value in req.model_dump(mode="json").items()):
            c.execute("UPDATE cases SET title=?,position=?,claims=?,research_date=? WHERE id=?", (req.title, req.position, req.claims, req.research_date.isoformat(), cid))
            bump_case(c, cid, "案件目标已更新")
        return case_for(c, cid, u)

@app.post("/api/v1/cases/{cid}/materials/text", status_code=201)
def text_material(cid: str, req: TextMaterial, u=Depends(user)):
    with connect(True) as c:
        case_for(c, cid, u)
        mid = store_material(c, cid, req.name, req.text, req.kind)
        audit(c, u["id"], "material.create", mid)
        return {"id": mid, "status": "ready"}

@app.post("/api/v1/cases/{cid}/materials", status_code=201)
async def upload(cid: str, file: UploadFile = File(), kind: str = Form("当事人提供材料"), u=Depends(user)):
    with connect() as c:
        case_for(c, cid, u)
    raw = await file.read(settings.MAX_FILE+1)
    if len(raw) > settings.MAX_FILE:
        raise HTTPException(413, "每个文件最多 20 MiB")
    name = Path(file.filename or "材料").name[:200]
    text, locations, warnings, status = await asyncio.to_thread(parse_file, name, raw)
    with connect(True) as c:
        case_for(c, cid, u)
        mid = store_material(c, cid, name, text, kind[:80], raw, locations, warnings, status)
        audit(c, u["id"], "material.upload", mid)
    return {"id": mid, "status": status, "warnings": warnings, "characters": len(text)}

@app.get("/api/v1/materials/{mid}")
def get_material(mid: str, u=Depends(user)):
    with connect() as c:
        m = c.execute("SELECT * FROM materials WHERE id=? AND withdrawn=0", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "材料不存在或已撤回")
        case_for(c, m["case_id"], u)
        return {**material_summary(m), "text": m["text"]}

@app.get("/api/v1/materials/{mid}/download")
def download_material(mid: str, u=Depends(user)):
    with connect() as c:
        m = c.execute("SELECT * FROM materials WHERE id=? AND withdrawn=0", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "材料不存在")
        case_for(c, m["case_id"], u)
        return FileResponse(settings.DATA/"uploads"/m["object_key"], filename=m["name"], media_type="application/octet-stream")

@app.post("/api/v1/materials/{mid}/versions", status_code=201)
def replace_material(mid: str, req: TextMaterial, u=Depends(user)):
    with connect(True) as c:
        m = c.execute("SELECT * FROM materials WHERE id=? AND active=1 AND withdrawn=0", (mid,)).fetchone()
        if not m:
            raise HTTPException(409, "材料版本已变化")
        case_for(c, m["case_id"], u)
        c.execute("UPDATE materials SET active=0 WHERE id=?", (mid,))
        nid = store_material(c, m["case_id"], req.name, req.text, req.kind, logical_id=m["logical_id"], version=m["version"]+1)
        return {"id": nid, "version": m["version"]+1}

@app.delete("/api/v1/materials/{mid}")
def withdraw_material(mid: str, u=Depends(user)):
    with connect(True) as c:
        m = c.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
        if not m:
            raise HTTPException(404, "材料不存在")
        case_for(c, m["case_id"], u)
        c.execute("UPDATE materials SET withdrawn=1,active=0 WHERE logical_id=?", (m["logical_id"],))
        bump_case(c, m["case_id"], "来源已撤回")
        audit(c, u["id"], "material.withdraw", mid)
    return {"status": "withdrawn", "retention": "POC 撤回阻止读取和导出；历史对象保留在本机备份范围，尚不实现生产级彻底删除。"}

@app.post("/api/v1/search")
def search(req: SearchInput, u=Depends(user)):
    try:
        return service("POST", "/search", req.model_dump(mode="json"))
    except Exception as exc:
        raise HTTPException(503, str(exc))

@app.post("/api/v1/search/facets")
def search_facets(req: FacetInput, u=Depends(user)):
    try:
        return service("POST", "/facets", req.model_dump(mode="json"))
    except Exception as exc:
        raise HTTPException(503, str(exc))

@app.get("/api/v1/judgments/{doc_id}")
def judgment(doc_id: str, u=Depends(user)):
    try:
        return service("GET", "/documents/"+doc_id)
    except Exception as exc:
        raise HTTPException(404, str(exc))

@app.get("/api/v1/law-sources")
def laws(u=Depends(user)):
    return law_sources()

@app.put("/api/v1/cases/{cid}/research-selections")
def select_research(cid: str, req: Selection, u=Depends(user)):
    with connect() as c:
        case_for(c, cid, u)
    selected_cases = [{"document_id": did, "case_number": service("GET", "/documents/"+did).get("case_number", "")} for did in req.doc_ids]
    if req.doc_ids:
        try:
            validate_research_selection(req.doc_ids, selected_cases)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    with connect(True) as c:
        case_for(c, cid, u)
        old = {r[0] for r in c.execute("SELECT doc_id FROM selections WHERE case_id=?", (cid,))}
        if old != set(req.doc_ids):
            c.execute("DELETE FROM selections WHERE case_id=?", (cid,))
            c.executemany("INSERT INTO selections VALUES (?,?)", [(cid, did) for did in set(req.doc_ids)])
            bump_case(c, cid, "选定研究来源已变化", source_changed=False)
    return {"doc_ids": req.doc_ids}

@app.get("/api/v1/cases/{cid}/similar-cases")
def get_similar_cases(cid: str, u=Depends(user)):
    with connect() as c:
        return similar_state(c, case_for(c, cid, u))

@app.put("/api/v1/cases/{cid}/similar-cases")
def update_similar_cases(cid: str, req: SimilarCaseInput, u=Depends(user)):
    with connect(True) as c:
        case = case_for(c, cid, u)
        check_version(c, cid, req.version)
        save_similar(c, cid, req)
        audit(c, u["id"], "similar_cases.options", cid)
        return similar_state(c, case)

@app.post("/api/v1/cases/{cid}/similar-cases/analyze")
def analyze_similar_cases(cid: str, req: SimilarCaseInput, u=Depends(user)):
    with connect() as c:
        case = case_for(c, cid, u)
        check_version(c, cid, req.version)
        state = similar_state(c, case)
    search_input = SearchInput(**{**req.options.model_dump(mode="json"), "query": req.options.query or state["suggested_query"]})
    try:
        response = service("POST", "/search", search_input.model_dump(mode="json"))
    except Exception as exc:
        raise HTTPException(503, "类案分析未完成，已保存的结果保持不变。" + str(exc))
    # Never hold a write transaction while embedding/searching. Recheck both
    # authorization and saved-options version before committing the response.
    with connect(True) as c:
        current = case_for(c, cid, u)
        check_version(c, cid, req.version)
        save_similar(c, cid, req, response=response, source_revision=case["source_revision"])
        audit(c, u["id"], "similar_cases.analyze", cid)
        return similar_state(c, current)

@app.put("/api/v1/cases/{cid}/similar-cases/{doc_id}/relevance")
def update_similar_case_relevance(cid: str, doc_id: str, req: SimilarCaseRelevance, u=Depends(user)):
    """Persist a case-specific exclusion without editing the original search snapshot or corpus."""
    with connect(True) as c:
        case = case_for(c, cid, u)
        check_version(c, cid, req.version)
        set_relevance(c, cid, doc_id, req.relevant, u["id"])
        audit(c, u["id"], "similar_cases.restore" if req.relevant else "similar_cases.exclude", cid+":"+doc_id)
        return similar_state(c, case)

@app.post("/api/v1/cases/{cid}/requests", status_code=202)
def create_run(cid: str, req: Goal, idempotency_key: str = Header(min_length=8, max_length=120), u=Depends(user)):
    try:
        plan = route(req.goal, req.workflow)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if "document.generate" in plan and not any(p in plan for p in ("case.assess", "evidence.gaps")):
        plan = ["document.generate"]
    document_type = req.document_type
    if req.workflow != "document.generate" and "起诉状" in req.goal:
        document_type = "complaint"
    elif req.workflow != "document.generate" and "代理意见" in req.goal:
        document_type = "opinion"
    elif req.workflow != "document.generate" and "内部" in req.goal and "报告" in req.goal:
        document_type = "internal"
    request_body = req.model_dump()
    for key in ("evidence_review", "evidence_selection", "evidence_artifact_id", "research_artifact_id", "research_selection", "document_template_id", "document_artifact_id", "document_content_hash"):
        if request_body[key] is None:
            request_body.pop(key)  # Preserve idempotency of pre-upgrade requests.
    if not request_body["document_tasks"]:
        request_body.pop("document_tasks")
    hashed = digest({"case_id": cid, **request_body})
    with connect(True) as c:
        case = case_for(c, cid, u)
        old = c.execute("SELECT id,request_hash FROM runs WHERE user_id=? AND idempotency_key=?", (u["id"], idempotency_key)).fetchone()
        if old:
            if old["request_hash"] != hashed:
                raise HTTPException(409, "同一幂等键不能用于不同请求")
            return {"id": old["id"], "reused": True}
        if c.execute("SELECT count(*) FROM runs WHERE user_id=? AND status IN ('queued','running')", (u["id"],)).fetchone()[0] >= 2:
            raise HTTPException(429, "当前已有两个待完成任务，请稍后提交")
        review_batch, review_snapshot = None, None
        evidence_selection, evidence_context = None, None
        research_context, research_selection = None, None
        document_context, document_template = None, None
        if "document.generate" in plan:
            if req.document_artifact_id:
                parent = artifact_for(c, req.document_artifact_id, u)
                document_context = draft_context(c, cid, req, parent)
                document_type = parent["structured"].get("document_type", document_type)
            elif req.document_content_hash:
                raise HTTPException(422, "请指定需要修改的文书版本。")
            template_id = req.document_template_id or (document_context or {}).get("structured", {}).get("template", {}).get("id")
            document_template = template_snapshot(c, cid, template_id, document_type)
        elif req.document_template_id or req.document_artifact_id or req.document_content_hash or req.document_tasks:
            raise HTTPException(422, "文书模板、版本与阅读重点仅用于文书生成。")
        if "research.followup" in plan:
            if not req.research_artifact_id:
                raise HTTPException(422, "请指定需要完善的研究版本。")
            research_context = artifact_for(c, req.research_artifact_id, u)
            if research_context["case_id"] != cid or research_context["kind"] != "research":
                raise HTTPException(422, "只能追问本案的研究成果。")
            reviewable(c, research_context)
            if c.execute("SELECT 1 FROM runs WHERE case_id=? AND status IN ('queued','running') AND json_extract(snapshot,'$.research_context.id')=?", (cid, research_context["id"])).fetchone():
                raise HTTPException(409, "上一条研究追问尚未完成，请等待后继续。")
        elif req.research_artifact_id:
            raise HTTPException(422, "研究版本只用于研究追问。")
        if req.research_selection:
            if "case.assess" not in plan:
                raise HTTPException(422, "本轮研究选案仅适用于类案研究。")
            research_selection = selection_snapshot(c, case, req.research_selection, max_cases=3, purpose="研究")
        if req.evidence_selection and ("evidence.gaps" not in plan or req.evidence_review):
            raise HTTPException(422, "本轮选案仅适用于证据分析，不能同时使用历史复核选案。")
        if "evidence.gaps" in plan and not req.evidence_review:
            evidence_selection = selection_snapshot(c, case, req.evidence_selection)
        if "evidence.followup" in plan:
            if not req.evidence_artifact_id:
                raise HTTPException(422, "请指定追问对应的证据分析版本。")
            evidence_context = artifact_for(c, req.evidence_artifact_id, u)
            if evidence_context["case_id"] != cid or evidence_context["kind"] != "evidence":
                raise HTTPException(422, "只能追问本案的证据分析。")
            reviewable(c, evidence_context)
            if c.execute("SELECT 1 FROM runs WHERE case_id=? AND status IN ('queued','running') AND json_extract(snapshot,'$.evidence_context.id')=?", (cid, evidence_context["id"])).fetchone():
                raise HTTPException(409, "上一条证据追问尚未完成，请等待后继续。")
        elif req.evidence_artifact_id:
            raise HTTPException(422, "证据分析版本只用于证据追问。")
        if req.evidence_review:
            if "evidence.gaps" not in plan:
                raise HTTPException(422, "复核类案选择只适用于证据分析任务")
            previous = run_for(c, req.evidence_review.run_id, u)
            if previous["case_id"] != cid:
                raise HTTPException(422, "不能使用其他案件的类案选择")
            if previous["status"] not in ("succeeded", "failed"):
                raise HTTPException(409, "请等待本轮分析结束后再调整类案")
            review_batch = batch_view(c, previous["id"])
            review_snapshot = json.loads(previous["snapshot"])
            if not review_batch or not set(req.evidence_review.doc_ids).issubset({x["document_id"] for x in review_batch["candidates"]}):
                raise HTTPException(422, "只能选择该轮真实检索候选中的 1 至 5 个类案")
            if previous["revision"] != case["revision"]:
                raise HTTPException(409, "本案资料已变化，请先基于当前材料自动分析，再复核选案")
            current_selection = selection_snapshot(c, case)
            if not set(req.evidence_review.doc_ids).issubset({i["document_id"] for i in current_selection["candidates"]}):
                raise HTTPException(422, "历史选案已不在当前类案筛选结果中，请重新选择。")
            review_batch["candidates"] = current_selection["candidates"]
            review_batch["retrieval"] = current_selection["retrieval"]
        context_material_ids = None
        if research_context:
            parent_run = run_for(c, research_context["run_id"], u)
            context_material_ids = json.loads(parent_run["snapshot"])["material_ids"]
            if req.material_ids is not None and set(req.material_ids) != set(context_material_ids):
                raise HTTPException(422, "追问使用原研究的材料范围；调整范围请新建研究。")
        all_m = [dict(m) for m in c.execute("SELECT * FROM materials WHERE case_id=? AND active=1 AND withdrawn=0 AND kind!='文书模板'", (cid,))]
        if document_template:
            all_m = [m for m in all_m if m["id"] != document_template["id"] and m["kind"] != "文书模板"]
        if req.material_ids is None and context_material_ids is None and any(m["status"] != "ready" for m in all_m):
            raise HTTPException(422, "存在未完整读取的材料，请在材料范围中明确选择可读材料；不可静默忽略缺页。")
        ids = req.material_ids if req.material_ids is not None else context_material_ids if context_material_ids is not None else review_snapshot["material_ids"] if review_snapshot else [m["id"] for m in all_m if m["status"] == "ready"]
        if not ids or not set(ids).issubset({m["id"] for m in all_m}):
            raise HTTPException(422, "请先上传材料，并选择本案可读材料范围")
        if len(ids) != len(set(ids)) or not set(req.document_tasks).issubset(ids):
            raise HTTPException(422, "每份材料只能选择一次，阅读重点必须对应本次所选材料。")
        if "case.assess" in plan and len(ids) > 3:
            raise HTTPException(422, "每轮研究最多选择 3 份材料，请调整本次材料范围。")
        chosen = [m for m in all_m if m["id"] in ids]
        if any(m["status"] != "ready" for m in chosen):
            raise HTTPException(422, "选定材料尚未完整解析，请补正文后重试")
        if len(chosen) > settings.MAX_MATERIALS or sum(len(m["text"]) for m in chosen) > settings.MAX_CHARS:
            raise HTTPException(413, "单次最多 20 份材料与 60,000 字，请明确缩小范围")
        config = c.execute("SELECT * FROM configurations ORDER BY version DESC LIMIT 1").fetchone()
        release = json.loads((settings.CORPUS/"release.json").read_text())
        snapshot = {"case": case, "material_ids": ids, "material_hashes": {m["id"]: m["text_hash"] for m in chosen}, "config": json.loads(config["content"]), "config_hash": config["content_hash"], "goal": req.goal, "document_type": document_type, "selected_doc_ids": [r[0] for r in c.execute("SELECT doc_id FROM selections WHERE case_id=?", (cid,))], "release": release, "workflow_version": settings.WORKFLOW_VERSION}
        snapshot["model_call_budget"] = 48 if "evidence.gaps" in plan and "case.assess" in plan else 32 if any(p in plan for p in ("evidence.gaps", "case.assess")) else 12
        if document_template:
            snapshot.update(document_policy=DOCUMENT_VERSION, document_template=document_template, document_tasks=req.document_tasks,
                            model_call_budget=100, document_context=document_context)
        snapshot["research_policy"] = settings.RESEARCH_VERSION
        if research_selection:
            result_release = research_selection["retrieval"].get("release_id")
            if result_release and result_release != release["release_id"]:
                raise HTTPException(409, "类案语料版本已变化，请重新分析类案。")
            snapshot["research_selection"] = research_selection
            snapshot["selected_doc_ids"] = research_selection["doc_ids"]
        if "case.assess" in plan and len(snapshot["selected_doc_ids"]) > 3:
            raise HTTPException(422, "每轮研究最多选择 3 个类案，请在结果与研究中调整后再开始。")
        if "case.assess" in plan and not snapshot["selected_doc_ids"]:
            similar = similar_state(c, case)
            if similar["response"] is not None and similar["needs_analysis"]:
                raise HTTPException(409, "类案筛选条件或材料已变化，请先重新分析类案，或在检索与选案中明确选择本轮案例。")
            if similar["response"] is not None and not similar["needs_analysis"]:
                if similar["response"].get("release_id") not in (None, release["release_id"]):
                    raise HTTPException(409, "类案语料版本已变化，请重新分析类案。")
                snapshot["research_candidates"] = similar["response"]["items"]
        if research_context:
            snapshot["research_context"] = {k: research_context[k] for k in ("id", "content_hash", "version", "structured", "sources")}
        snapshot["evidence_policy"] = {"version": settings.EVIDENCE_VERSION, "max_cases": settings.EVIDENCE_MAX_CASES, "concurrency": settings.EVIDENCE_CONCURRENCY}
        if evidence_selection:
            result_release = evidence_selection["retrieval"].get("release_id")
            if result_release and result_release != release["release_id"]:
                raise HTTPException(409, "类案语料版本已变化，请重新分析类案。")
            snapshot["evidence_selection"] = evidence_selection
        if evidence_context:
            history = [artifact_for(c, row["id"], u) for row in c.execute("SELECT id FROM artifacts WHERE case_id=? AND kind='evidence_answer' AND json_extract(structured,'$.evidence_artifact_id')=? ORDER BY created_at DESC LIMIT 3", (cid, evidence_context["id"])).fetchall()]
            snapshot["evidence_context"] = {"id": evidence_context["id"], "content_hash": evidence_context["content_hash"],
                "summary": evidence_context["body"], "sources": evidence_context["sources"],
                "evidence_tasks": evidence_context["evidence_view"]["additional_evidence"],
                "conversation": [{"question": a["structured"]["question"][:600], "answer": a["body"][:1000]} for a in reversed(history) if a["checks"].get("passed") and not a["stale"]]}
        if review_batch:
            if review_snapshot["material_hashes"] != snapshot["material_hashes"] or review_snapshot["release"]["release_id"] != release["release_id"]:
                raise HTTPException(409, "材料范围或语料版本已变化，请重新自动分析")
            snapshot["evidence_review"] = {**req.evidence_review.model_dump(), "candidates": review_batch["candidates"], "retrieval": review_batch["retrieval"]}
        snapshot["law_sources"] = law_sources()
        snapshot["law_catalog_hash"] = digest(snapshot["law_sources"])
        if "document.generate" in plan and not snapshot["selected_doc_ids"]:
            for previous in c.execute("SELECT a.*,r.snapshot FROM artifacts a JOIN runs r ON r.id=a.run_id WHERE a.case_id=? AND a.kind='research' AND a.case_revision=? AND a.reviewed_by IS NOT NULL ORDER BY a.created_at DESC", (cid,case["revision"])).fetchall():
                previous_snapshot = json.loads(previous["snapshot"])
                if json.loads(previous["checks"]).get("passed") and previous_snapshot["material_hashes"] == snapshot["material_hashes"] and previous_snapshot["config_hash"] == snapshot["config_hash"] and previous_snapshot.get("law_catalog_hash") == snapshot["law_catalog_hash"]:
                    previous_ids = sorted({s["doc_id"] for s in json.loads(previous["sources"]) if s["source_kind"] == "judgment"})
                    if len(previous_ids) > 5:
                        continue
                    snapshot["selected_doc_ids"] = previous_ids
                    snapshot["reviewed_research_id"] = previous["id"]
                    break
        rid = uid("run")
        c.execute("INSERT INTO runs (id,case_id,user_id,goal,plan,status,snapshot,config_id,revision,idempotency_key,request_hash,stage,created_at,updated_at) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,'等待执行',?,?)", (rid, cid, u["id"], req.goal, dumps(plan), dumps(snapshot), config["id"], case["revision"], idempotency_key, hashed, now(), now()))
        event(c, rid, "run.queued", {"plan": plan, "material_count": len(chosen)})
        audit(c, u["id"], "run.create", rid)
    return {"id": rid, "plan": plan}

@app.get("/api/v1/runs/{rid}")
def run(rid: str, u=Depends(user)):
    with connect() as c:
        r = run_for(c, rid, u)
        for key in ("snapshot", "plan", "wait_payload", "resume_payload"):
            r[key] = json.loads(r[key]) if r[key] else None
        r["events"] = [{**dict(e), "payload": json.loads(e["payload"])} for e in c.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (rid,))]
        r["artifacts"] = [a[0] for a in c.execute("SELECT id FROM artifacts WHERE run_id=? ORDER BY created_at", (rid,))]
        r["model_calls"] = [dict(a) for a in c.execute("SELECT id,operation_key,status,usage,elapsed_ms,error,created_at FROM model_calls WHERE run_id=?", (rid,))]
        return r

@app.get("/api/v1/runs/{rid}/evidence")
def evidence_progress(rid: str, u=Depends(user)):
    with connect() as c:
        r = run_for(c, rid, u)
        return {"run_id": rid, "run_status": r["status"], "stage": r["stage"], "error": r["error"], "batch": batch_view(c, rid)}

@app.get("/api/v1/runs/{rid}/research")
def research_progress(rid: str, u=Depends(user)):
    with connect() as c:
        r = run_for(c, rid, u)
        return {"run_id": rid, "run_status": r["status"], "stage": r["stage"], "error": r["error"], "batch": research_batch_view(c, rid)}


@app.get("/api/v1/document-templates")
def document_templates(u=Depends(user)):
    return builtin_templates()


@app.get("/api/v1/runs/{rid}/document")
def document_progress(rid: str, u=Depends(user)):
    from .document_agents import progress
    with connect() as c:
        r = run_for(c, rid, u)
        return {"run_id": rid, "stage": r["stage"], "agents": progress(c, rid)}


@app.get("/api/v1/runs/{rid}/events")
def events(rid: str, request: Request, last_event_id: str = Header(default="0"), u=Depends(user)):
    with connect() as c:
        run_for(c, rid, u)
    async def stream():
        seq = int(last_event_id) if last_event_id.isdigit() else 0
        while not await request.is_disconnected():
            try:
                current_user = user(request)
                with connect() as c:
                    r = run_for(c, rid, current_user)
                    rows = c.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (rid, seq)).fetchall()
            except HTTPException:
                return
            for e in rows:
                seq = e["seq"]
                yield f"id: {seq}\nevent: progress\ndata: {dumps({'type':e['type'],'payload':json.loads(e['payload'])})}\n\n"
            if r["status"] in ("succeeded", "failed", "cancelled", "waiting_input"):
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

@app.post("/api/v1/runs/{rid}/resume", status_code=202)
def resume(rid: str, req: Resume, u=Depends(user)):
    answer = dumps(req.answer)
    with connect(True) as c:
        r = run_for(c, rid, u)
        if r["resume_payload"]:
            if r["resume_payload"] == answer:
                return {"id": rid, "reused": True}
            raise HTTPException(409, "本等待项已有不同的回答")
        if r["status"] != "waiting_input":
            raise HTTPException(409, "任务当前不等待输入")
        case = case_for(c, r["case_id"], u)
        if case["revision"] != r["revision"]:
            raise HTTPException(409, "材料已更新，请新建任务")
        wait = json.loads(r["wait_payload"])
        if wait["kind"] == "case_selection":
            ids = req.answer.get("doc_ids")
            if not isinstance(ids, list) or len(ids) > 5 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or not set(ids).issubset({a["document_id"] for a in wait["candidates"]}):
                raise HTTPException(422, "请在本次候选中选择最多 5 个不同类案")
            if ids:
                try:
                    validate_research_selection(ids, wait["candidates"])
                except ValueError as exc:
                    raise HTTPException(422, str(exc))
        elif req.answer.get("workflow") not in ("case.timeline", "evidence.gaps", "case.assess", "document.generate"):
            raise HTTPException(422, "请选择受支持的任务")
        c.execute("UPDATE runs SET resume_payload=?,status='queued',stage='等待继续',updated_at=? WHERE id=?", (answer, now(), rid))
        event(c, rid, "input.submitted", {"kind": wait["kind"]})
    return {"id": rid}

@app.post("/api/v1/runs/{rid}/cancel")
def cancel(rid: str, u=Depends(user)):
    with connect(True) as c:
        r = run_for(c, rid, u)
        if r["status"] not in ("succeeded", "failed", "cancelled"):
            c.execute("UPDATE runs SET cancel_requested=1,status='cancelled',stage='已取消',updated_at=? WHERE id=?", (now(), rid))
            event(c, rid, "run.cancelled", {})
            r["status"] = "cancelled"
    return {"id": rid, "status": r["status"]}

@app.post("/api/v1/runs/{rid}/retry", status_code=202)
def retry_original(rid: str, u=Depends(user)):
    with connect(True) as c:
        r = run_for(c, rid, u)
        case = case_for(c, r["case_id"], u)
        if r["status"] != "failed" or r["cancel_requested"] or r["recovery_count"] >= 2:
            raise HTTPException(409, "仅可在恢复预算内恢复失败任务；其他情况请新建任务")
        if case["revision"] != r["revision"] or json.loads(r["snapshot"])["workflow_version"] != settings.WORKFLOW_VERSION:
            raise HTTPException(409, "输入或工作流已变化，请基于当前资料新建任务")
        evidence_version = c.execute("SELECT version FROM evidence_batches WHERE run_id=?", (rid,)).fetchone()
        if evidence_version and evidence_version["version"] != settings.EVIDENCE_VERSION:
            raise HTTPException(409, "证据分析流程已更新，请新建分析；原逐案结果仍可查看")
        research_version = c.execute("SELECT version FROM research_batches WHERE run_id=?", (rid,)).fetchone()
        if research_version and research_version["version"] != settings.RESEARCH_VERSION:
            raise HTTPException(409, "研究流程已更新，请新建研究；原逐案结果仍可查看")
        if c.execute("SELECT count(*) FROM runs WHERE user_id=? AND status IN ('queued','running')", (u["id"],)).fetchone()[0] >= 2:
            raise HTTPException(429, "当前已有两个待完成任务")
        c.execute("UPDATE runs SET status='queued',error=NULL,stage='等待按原输入恢复',recovery_count=recovery_count+1,updated_at=? WHERE id=?", (now(),rid))
        event(c,rid,"run.retry_requested",{"same_snapshot":True})
    return {"id":rid,"same_snapshot":True}

@app.get("/api/v1/artifacts/{aid}")
def artifact(aid: str, u=Depends(user)):
    with connect() as c:
        return artifact_for(c, aid, u)

def editable_checks(a, body):
    known = {s["id"] for s in a["sources"]}
    used = set(re.findall(r"\[((?:M|J|L)[A-Za-z0-9_]+)\]", body))
    issues = []
    if not used.issubset(known):
        issues.append("正文包含未知来源编号")
    if not used:
        issues.append("正文需要保留可核验来源；修正错误引文时可改用已有正确来源")
    for item in a["structured"].get("unresolved", []):
        if item not in body:
            issues.append("编辑删除了待核事项")
            break
    return {"passed": not issues, "issues": issues, "semantic_review": "pending"}

@app.post("/api/v1/artifacts/{aid}/versions", status_code=201)
def edit(aid: str, req: EditArtifact | EditTimeline, u=Depends(user)):
    with connect(True) as c:
        a = artifact_for(c, aid, u)
        if req.content_hash != a["content_hash"] or c.execute("SELECT 1 FROM artifacts WHERE parent_id=?", (aid,)).fetchone():
            raise HTTPException(409, "该成果已有新版本，请重新加载")
        if a["kind"] == "timeline":
            if not isinstance(req, EditTimeline):
                raise HTTPException(422, "请通过时间线编辑器保存事件")
            latest = c.execute("SELECT id FROM artifacts WHERE case_id=? AND kind='timeline' ORDER BY created_at DESC, rowid DESC LIMIT 1", (a["case_id"],)).fetchone()
            if latest["id"] != aid:
                raise HTTPException(409, "时间线已有新版本，请重新加载后编辑")
            try:
                body, structured, a["sources"] = edited_timeline(c, a, req.facts)
                check_sources(c, a["case_id"], a["sources"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        else:
            if not isinstance(req, EditArtifact):
                raise HTTPException(422, "此成果不支持时间线事件编辑")
            body, structured = req.body, {**a["structured"], "edited_body": req.body}
        checks = editable_checks(a, body)
        nid = uid("art")
        c.execute("INSERT INTO artifacts (id,case_id,run_id,kind,version,parent_id,title,body,structured,sources,content_hash,checks,completeness,case_revision,created_at,operation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (nid,a["case_id"],a["run_id"],a["kind"],a["version"]+1,aid,a["title"],body,dumps(structured),dumps(a["sources"]),digest(body),dumps(checks),a["completeness"],a["case_revision"],now(),"edit:"+nid))
        audit(c, u["id"], "artifact.edit", nid)
        if a["kind"] == "document" and checks["passed"]:
            try:
                check_sources(c, a["case_id"], a["sources"])
                archive_draft(c, nid)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        return artifact_for(c, nid, u)

def reviewable(c, a):
    if a["stale"]:
        raise HTTPException(409, "资料范围已变化，请重新生成当前成果")
    if not a["checks"]["passed"]:
        raise HTTPException(422, "程序检查未通过，不能复核或导出")
    try:
        check_sources(c, a["case_id"], a["sources"])
    except Exception as exc:
        raise HTTPException(422, str(exc))

@app.post("/api/v1/artifacts/{aid}/reviews")
def review(aid: str, req: Review, u=Depends(user)):
    with connect(True) as c:
        a = artifact_for(c, aid, u)
        if req.content_hash != a["content_hash"] or not req.confirmed:
            raise HTTPException(409, "请确认当前精确版本的复核")
        reviewable(c, a)
        c.execute("UPDATE artifacts SET reviewed_by=?,reviewed_at=? WHERE id=?", (u["id"], now(), aid))
        audit(c, u["id"], "artifact.review", aid)
    return {"review_status": "reviewed"}

@app.get("/api/v1/artifacts/{aid}/export")
def export(aid: str, u=Depends(user)):
    with connect(True) as c:
        a = artifact_for(c, aid, u)
        reviewable(c, a)
        if not a["reviewed_by"]:
            raise HTTPException(409, "请先复核该成果版本")
        r = c.execute("SELECT snapshot,config_id,created_at FROM runs WHERE id=?", (a["run_id"],)).fetchone()
        snap = json.loads(r["snapshot"])
        appendix = "\n\n---\n\n## 来源与版本附录\n\n"+"\n\n".join(f"[{s['id']}] {s['name']} · {s.get('location','')}\n\n> {s['quote']}"+("\n\n来源："+s["url"] if s.get("url") else "") for s in a["sources"])
        appendix += f"\n\n生成时间：{a['created_at']}\n\n复核时间：{a['reviewed_at']}\n\n成果版本：{a['id']} / v{a['version']}\n\n配置：{r['config_id']}；语料：{snap['release']['release_id']}\n\n资料范围：{', '.join(snap['material_ids'])}\n\n内容 SHA-256：{a['content_hash']}\n"
        body = a["body"]+appendix
        c.execute("INSERT INTO exports VALUES (?,?,?,?,?)", (uid("export"), aid, u["id"], digest(body), now()))
        audit(c, u["id"], "artifact.export", aid)
    return Response(body, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="caiyan-{aid}.md"'})


def reference_list(c, cid, u):
    result = []
    for row in c.execute("SELECT id,artifact_id,answer_ids,name,content_hash,created_at FROM reference_documents WHERE case_id=? AND deleted_at IS NULL ORDER BY created_at DESC", (cid,)).fetchall():
        entry = dict(row)
        artifacts = [artifact_for(c, aid, u) for aid in [entry["artifact_id"], *json.loads(entry.pop("answer_ids"))]]
        entry.update(stale=any(a["stale"] for a in artifacts), available=all(a["checks"].get("passed") for a in artifacts),
                     analysis_created_at=artifacts[0]["created_at"], version=artifacts[0]["version"], kind=artifacts[0]["kind"], answer_count=len(artifacts[0]["structured"].get("conversation", [])) if artifacts[0]["kind"] == "research" else len(artifacts)-1)
        entry["review_status"] = artifacts[0]["review_status"]
        result.append(entry)
    return result


@app.post("/api/v1/artifacts/{aid}/reference-documents", status_code=201)
def create_reference_document(aid: str, req: EvidenceReference, idempotency_key: str = Header(min_length=8, max_length=100), u=Depends(user)):
    from .evidence_documents import build_reference
    request_hash = digest({"artifact_id": aid, **req.model_dump()})
    with connect() as c:
        a = artifact_for(c, aid, u)
        if a["kind"] not in ("evidence", "research", "document"):
            raise HTTPException(422, "此入口用于分析成果或文书的 Word 文档。")
        if req.content_hash != a["content_hash"]:
            raise HTTPException(409, "分析版本已变化，请刷新后重试。")
        old = c.execute("SELECT id,request_hash,deleted_at FROM reference_documents WHERE user_id=? AND idempotency_key=?", (u["id"], idempotency_key)).fetchone()
        if old:
            if old["deleted_at"]:
                raise HTTPException(409, "该文档已删除，请使用新的请求保存新文档。")
            if old["request_hash"] != request_hash:
                raise HTTPException(409, "同一幂等键不能用于不同文档。")
            return next(item for item in reference_list(c, a["case_id"], u) if item["id"] == old["id"])
        reviewable(c, a)
        case = case_for(c, a["case_id"], u)
        if a["kind"] == "document":
            from .document_workspace import word_bytes
            draft_content = word_bytes(c, a)
        answers = []
        # Evidence references contain only the material list, supplement table
        # and concise questions. Legacy include_answers requests stay accepted.
        batch = research_batch_view(c, a["run_id"]) if a["kind"] == "research" else None
    created_at = now()
    if a["kind"] == "research":
        from .research_documents import build_reference
    content = draft_content if a["kind"] == "document" else build_reference(case, a, batch, answers, created_at, req.timezone)
    stamp = datetime.fromisoformat(created_at).astimezone(ZoneInfo(req.timezone)).strftime("%Y%m%d_%H%M%S_%f_%Z")
    doc_id = uid("refdoc")
    name = (a["title"]+"_" if a["kind"] == "document" else "研究报告_" if a["kind"] == "research" else "证据分析参考_")+stamp+".docx"
    with connect(True) as c:
        # Re-check after rendering; material withdrawal or edits must win this race.
        for item in [a, *answers]:
            current = artifact_for(c, item["id"], u)
            reviewable(c, current)
        if not req.save_to_materials:
            return reference_response(content, name)
        old = c.execute("SELECT id,request_hash,deleted_at FROM reference_documents WHERE user_id=? AND idempotency_key=?", (u["id"], idempotency_key)).fetchone()
        if old:
            if old["deleted_at"]:
                raise HTTPException(409, "该文档已删除，请使用新的请求保存新文档。")
            if old["request_hash"] != request_hash:
                raise HTTPException(409, "同一幂等键不能用于不同文档。")
            doc_id = old["id"]
        else:
            c.execute("INSERT INTO reference_documents(id,case_id,artifact_id,answer_ids,name,content,content_hash,user_id,created_at,idempotency_key,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (doc_id, case["id"], aid,
                dumps([answer["id"] for answer in answers]), name, content,
                hashlib.sha256(content).hexdigest(), u["id"], created_at, idempotency_key, request_hash))
            audit(c, u["id"], a["kind"]+".reference_document", doc_id)
        return next(item for item in reference_list(c, case["id"], u) if item["id"] == doc_id)


def reference_response(content, name):
    from urllib.parse import quote
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": "attachment; filename=\"research-reference.docx\"; filename*=UTF-8''"+quote(name)})


@app.delete("/api/v1/reference-documents/{did}")
def delete_reference_document(did: str, u=Depends(user)):
    with connect(True) as c:
        row = c.execute("SELECT case_id FROM reference_documents WHERE id=?", (did,)).fetchone()
        if not row:
            raise HTTPException(404, "参考文档不存在。")
        case_for(c, row["case_id"], u)
        c.execute("UPDATE reference_documents SET deleted_at=COALESCE(deleted_at,?),content=X'' WHERE id=?", (now(), did))
        audit(c, u["id"], "reference_document.delete", did)
    return {"id": did, "status": "deleted"}


@app.get("/api/v1/reference-documents/{did}/download")
def download_reference_document(did: str, u=Depends(user)):
    from urllib.parse import quote
    with connect() as c:
        row = c.execute("SELECT * FROM reference_documents WHERE id=? AND deleted_at IS NULL", (did,)).fetchone()
        if not row:
            raise HTTPException(404, "参考文档不存在。")
        case_for(c, row["case_id"], u)
        for aid in [row["artifact_id"], *json.loads(row["answer_ids"])]:
            a = artifact_for(c, aid, u)
            if not a["checks"].get("passed"):
                raise HTTPException(422, "来源已撤回或内容检查未通过，文档已停止下载。")
            try:
                check_sources(c, a["case_id"], a["sources"])
            except Exception as exc:
                raise HTTPException(422, str(exc))
        return reference_response(row["content"], row["name"])

@app.post("/api/v1/cases/{cid}/feedback", status_code=201)
def feedback(cid: str, req: Feedback, u=Depends(user)):
    with connect(True) as c:
        case_for(c, cid, u)
        if req.target_id.startswith("art_") or c.execute("SELECT 1 FROM artifacts WHERE id=?", (req.target_id,)).fetchone():
            a = artifact_for(c, req.target_id, u)
            if a["case_id"] != cid:
                raise HTTPException(404, "成果不属于本案")
            if req.rating == "unsupported":
                checks = {**a["checks"], "passed": False, "semantic_review": "rejected", "issues": ["内容复核不通过："+(req.note or "推论或引用支持不足，请修订后重新复核。")[:2000]]}
                c.execute("UPDATE artifacts SET checks=?,reviewed_by=NULL,reviewed_at=NULL WHERE id=?", (dumps(checks), req.target_id))
        fid = uid("feedback")
        c.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?,?)", (fid,cid,u["id"],req.target_id,req.rating,req.note,now()))
    return {"id": fid}

@app.get("/api/v1/admin/configurations")
def configs(u=Depends(admin)):
    with connect() as c:
        return [{**dict(r), "content": json.loads(r["content"])} for r in c.execute("SELECT * FROM configurations ORDER BY version DESC")]

@app.post("/api/v1/admin/configurations/tests", status_code=202)
def test_config(req: WritingConfig, u=Depends(admin)):
    tid, content = uid("test"), req.model_dump()
    with connect(True) as c:
        c.execute("INSERT INTO config_tests VALUES (?,?,?,?,?,?)", (tid, digest(content), dumps(content), "queued", None, now()))
    return {"id": tid}

@app.get("/api/v1/admin/configurations/tests/{tid}")
def config_test(tid: str, u=Depends(admin)):
    with connect() as c:
        r = c.execute("SELECT * FROM config_tests WHERE id=?", (tid,)).fetchone()
        if not r:
            raise HTTPException(404, "试运行不存在")
        return {**dict(r), "result": json.loads(r["result"]) if r["result"] else None}

@app.post("/api/v1/admin/configurations/publish", status_code=201)
def publish(req: PublishConfig, u=Depends(admin)):
    content = req.content.model_dump()
    with connect(True) as c:
        latest = c.execute("SELECT max(version) FROM configurations").fetchone()[0]
        if latest != req.base_version:
            raise HTTPException(409, "已有新发布版本，请重新比较")
        test = c.execute("SELECT * FROM config_tests WHERE id=?", (req.test_id,)).fetchone()
        if not req.reviewed or not test or test["status"] != "passed" or test["content_hash"] != digest(content):
            raise HTTPException(422, "必须使用相同内容的成功试运行，并完成人工比较")
        tested_profile = json.loads(test["result"]).get("model_profile")
        if tested_profile != json.loads((settings.DATA/"model-profile.json").read_text()):
            raise HTTPException(422, "模型版本已变化，请重新试运行")
        cid = uid("config")
        c.execute("INSERT INTO configurations VALUES (?,?,?,?,?,?,?)", (cid,latest+1,dumps(content),digest(content),req.test_id,None,now()))
        audit(c, u["id"], "configuration.publish", cid)
    return {"id": cid, "version": latest+1}

@app.get("/api/v1/admin/runs")
def diagnostics(u=Depends(admin)):
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT id,status,stage,config_id,recovery_count,created_at,updated_at FROM runs ORDER BY created_at DESC LIMIT 100")]

@app.get("/api/v1/admin/users")
def users(u=Depends(admin)):
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT id,username,name,role,disabled FROM users")]

@app.get("/healthz")
def live():
    return {"status": "alive"}

dist = settings.ROOT/"frontend/dist"
if (dist/"assets").exists():
    app.mount("/assets", StaticFiles(directory=dist/"assets"), name="assets")

@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "API 不存在")
    if not (dist/"index.html").exists():
        raise HTTPException(503, "请先运行 npm --prefix frontend run build")
    return FileResponse(dist/"index.html")
