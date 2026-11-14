"""Small self-hosted gateway using LiteLLM; fixed local aliases, no cloud fallback."""
import os
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_LOG"] = "ERROR"
os.environ["DO_NOT_TRACK"] = "1"
import asyncio
import json
import secrets
from urllib.parse import urlparse
import httpx
import numpy as np
from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel, Field, ConfigDict
import litellm
from . import settings

litellm.telemetry = False
litellm.num_retries = 0
gate = asyncio.Semaphore(settings.EVIDENCE_CONCURRENCY)

def auth(x_internal_key: str = Header(default="")):
    if not secrets.compare_digest(x_internal_key, settings.internal_key()):
        raise HTTPException(401, "Internal service credential required")

class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    messages: list[dict[str, str]] = Field(max_length=4)
    max_tokens: int = Field(default=1600, ge=100, le=4000)
    output_schema: dict | None = None

class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    input: str = Field(min_length=1, max_length=5000)

app = FastAPI(title="裁研 LiteLLM local gateway", dependencies=[Depends(auth)])

def profile():
    if urlparse(settings.OLLAMA).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(503, "This POC only permits loopback Ollama endpoints")
    try:
        r = httpx.get(settings.OLLAMA+"/api/tags", timeout=4, trust_env=False)
        r.raise_for_status()
        by_name = {m["name"]: m for m in r.json()["models"]}
        result = {"chat": {"alias": settings.MODEL_ALIAS, "model": settings.MODEL, "digest": by_name[settings.MODEL]["digest"]},
                  "embedding": {"alias": settings.EMBED_ALIAS, "model": settings.EMBED_MODEL, "digest": by_name[settings.EMBED_MODEL]["digest"], "dimensions": settings.DIMENSIONS},
                  "gateway": "litellm-1.101.0", "endpoint": settings.OLLAMA, "retries": 0, "cloud_fallback": False}
        frozen = settings.DATA / "model-profile.json"
        if frozen.exists() and json.loads(frozen.read_text()) != result:
            raise HTTPException(503, "本地模型映射已变化，请核对后创建新版本配置。")
        if not frozen.exists():
            frozen.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except (httpx.HTTPError, KeyError):
        raise HTTPException(503, "Ollama 或批准的 Qwen 模型不可用。")

@app.get("/health")
def health():
    return {"status": "ready", "profile": profile()}

@app.post("/v1/chat/completions")
async def chat(req: ChatRequest):
    if req.model != settings.MODEL_ALIAS:
        raise HTTPException(422, "未批准的模型别名")
    # Conservative Chinese character bound; no silent truncation.
    if sum(len(m.get("content", "")) for m in req.messages) > 15000:
        raise HTTPException(413, "模型上下文超过本 POC 的字符预算，请分批。")
    profile()
    async with gate:
        try:
            response = await litellm.acompletion(model="ollama_chat/"+settings.MODEL, api_base=settings.OLLAMA,
                messages=req.messages, temperature=0, max_tokens=req.max_tokens, num_ctx=16384,
                response_format={"type":"json_schema","json_schema":{"name":"caiyan_artifact","schema":req.output_schema}} if req.output_schema else {"type": "json_object"}, timeout=240, num_retries=0, stream=False)
            result = response.model_dump()
            if result["choices"][0]["finish_reason"] != "stop":
                raise HTTPException(502, "模型输出被截断；未提交为有效成果。")
            return result
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, "本机模型调用失败："+type(exc).__name__)

@app.post("/v1/embeddings")
async def embedding(req: EmbedRequest):
    if req.model != settings.EMBED_ALIAS:
        raise HTTPException(422, "未批准的嵌入别名")
    profile()
    async with gate:
        try:
            response = await litellm.aembedding(model="ollama/"+settings.EMBED_MODEL, api_base=settings.OLLAMA,
                input=[req.input], dimensions=settings.DIMENSIONS, truncate=False, timeout=120, num_retries=0)
            vector = np.asarray(response.data[0]["embedding"], dtype=np.float32)
            norm = float(np.linalg.norm(vector))
            if len(vector) != settings.DIMENSIONS or not np.isfinite(norm) or norm == 0:
                raise ValueError("Invalid embedding")
            return {"model": settings.EMBED_ALIAS, "data": [{"index": 0, "embedding": (vector/norm).tolist()}]}
        except Exception as exc:
            raise HTTPException(502, "嵌入调用失败："+type(exc).__name__)
