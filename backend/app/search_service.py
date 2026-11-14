"""One process owns the copied embedded Qdrant store; API and worker use HTTP."""
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, HTTPException, Header, Depends
from qdrant_client import QdrantClient, models
from . import settings
from .contracts import SearchInput, FacetInput
from .search_filters import attach_filters, predicates, facet_response
from .db import digest
from scripts.prepare_corpus import terms

def authorize(x_internal_key: str = Header(default="")):
    import secrets
    if not secrets.compare_digest(x_internal_key, settings.internal_key()):
        raise HTTPException(401, "Internal service credential required")

class SearchEngine:
    def __init__(self):
        self.release = json.loads((settings.CORPUS / "release.json").read_text())
        self.client = QdrantClient(path=str(settings.CORPUS / "qdrant"))
        self.lock = threading.Lock()
        self.vector_name = next(iter(self.client.get_collection("judgment_passages").config.params.vectors))

    def sql(self):
        c = sqlite3.connect((settings.CORPUS / "judgments.sqlite3").as_uri() + "?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        attach_filters(c, settings.CORPUS)
        return c

    def facets(self, req):
        with self.sql() as c:
            return facet_response(c, req, self.release)

    def document(self, doc_id):
        with self.sql() as c:
            r = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not r:
                raise HTTPException(404, "来源不存在")
            d = dict(r)
            d["passages"] = [self.passage(p[0], c) for p in c.execute("SELECT id FROM passages WHERE document_id=?", (doc_id,)).fetchall()]
            d["release_id"] = self.release["release_id"]
            return d

    def passage(self, pid, c):
        row = c.execute("SELECT p.*,d.title,d.case_number,d.court,d.judgment_date,d.cause,d.province,d.content,d.sha256,d.source_row FROM passages p JOIN documents d ON d.id=p.document_id WHERE p.id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "片段不存在")
        d = dict(row)
        body = d.pop("content")
        d["quote"] = body[d["start"]:d["end"]]
        if digest(body) != d["sha256"] or not d["quote"]:
            raise HTTPException(503, "来源校验失败")
        d["source_kind"] = "judgment"
        d["release_id"] = self.release["release_id"]
        return d

    def search(self, req):
        started = time.monotonic()
        where, params = predicates(req, include_query=True)
        filters_present = where != "1=1"
        warnings, lexical_ids, vector_ids = [], [], []
        with self.sql() as c:
            allowed = None
            if filters_present:
                allowed = {r[0] for r in c.execute(f"SELECT p.id FROM passages p JOIN documents d ON d.id=p.document_id WHERE {where}", params)}
                if not allowed:
                    return self.response([], req, "exact" if req.mode == "case_number" else "lexical" if req.mode == "keyword" else req.mode, [], started)
            if req.mode == "case_number":
                lexical_ids = sorted(allowed)
            elif req.mode != "semantic":
                qterms = terms(req.query)
                if not qterms:
                    raise HTTPException(422, "请输入中文、英文或数字检索词")
                with sqlite3.connect((settings.CORPUS / "lexical.sqlite3").as_uri()+"?mode=ro", uri=True) as lex:
                    lex.execute("ATTACH DATABASE ? AS corpus", ((settings.CORPUS / "judgments.sqlite3").as_uri()+"?mode=ro",))
                    attach_filters(lex, settings.CORPUS)
                    expression = " OR ".join('"'+t+'"' for t in qterms[:100])
                    rows = lex.execute(f"SELECT f.passage_id FROM passages_fts f JOIN corpus.documents d ON d.id=f.doc_id WHERE passages_fts MATCH ? AND {where} ORDER BY bm25(passages_fts) LIMIT 100", [expression, *params])
                    lexical_ids = [r[0] for r in rows]
            mode = "exact" if req.mode == "case_number" else "lexical"
            if req.mode in ("semantic", "hybrid"):
                try:
                    r = httpx.post(settings.GATEWAY+"/v1/embeddings", json={"model": settings.EMBED_ALIAS, "input": settings.QUERY_INSTRUCTION+req.query}, headers={"X-Internal-Key": settings.internal_key()}, timeout=120, trust_env=False)
                    r.raise_for_status()
                    vector = r.json()["data"][0]["embedding"]
                    qfilter = models.Filter(must=[models.FieldCondition(key="passage_id", match=models.MatchAny(any=list(allowed)))]) if allowed is not None else None
                    with self.lock:
                        hits = self.client.query_points("judgment_passages", query=vector, using=self.vector_name, query_filter=qfilter, limit=100, with_payload=True).points
                    vector_ids = [h.payload["passage_id"] for h in hits if h.payload]
                    mode = req.mode
                except (httpx.HTTPError, KeyError, ValueError, RuntimeError) as e:
                    if req.mode == "semantic":
                        raise HTTPException(503, "语义检索暂不可用，请重试或选择其他检索方式。") from e
                    warnings.append("语义检索暂不可用，已返回保持全部筛选条件的词面结果（"+type(e).__name__+"）。")
            scores = {}
            for ids in (lexical_ids, vector_ids):
                for rank, pid in enumerate(ids):
                    if allowed is not None and pid not in allowed:
                        continue
                    scores[pid] = scores.get(pid, 0) + 1/(60+rank+1)
            results, seen = [], set()
            for pid in sorted(scores, key=scores.get, reverse=True):
                item = self.passage(pid, c)
                if item["document_id"] in seen:
                    continue
                # Reapply every hard filter while resolving original text.
                if not c.execute(f"SELECT 1 FROM documents d WHERE d.id=? AND {where}", [item["document_id"], *params]).fetchone():
                    continue
                seen.add(item["document_id"])
                item["rank"] = len(results)+1
                item["retrieval_score"] = round(scores[pid], 6)
                results.append(item)
                if len(results) >= req.limit:
                    break
        return self.response(results, req, mode, warnings, started)

    def response(self, items, req, mode, warnings, started):
        return {"items": items, "release_id": self.release["release_id"], "scope": self.release["scope"],
                "applied_filters": req.model_dump(mode="json"), "retrieval_mode": mode, "degraded": bool(warnings),
                "warnings": warnings, "count_scope": "bounded_candidates", "candidate_count": len(items), "elapsed_ms": round((time.monotonic()-started)*1000)}

engine = None
@asynccontextmanager
async def lifespan(app):
    global engine
    engine = SearchEngine()
    yield
    engine.client.close()

app = FastAPI(title="裁研本地检索服务", lifespan=lifespan, dependencies=[Depends(authorize)])
@app.get("/health")
def health():
    return engine.release
@app.post("/search")
def search(req: SearchInput):
    return engine.search(req)
@app.post("/facets")
def facets(req: FacetInput):
    return engine.facets(req)
@app.get("/documents/{doc_id}")
def document(doc_id: str):
    return engine.document(doc_id)
