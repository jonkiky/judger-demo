"""Local POC settings. The source index is never opened for writing."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.getenv("CAIYAN_DATA_DIR", str(ROOT / "data"))).resolve()
INDEX = Path(os.getenv("CAIYAN_INDEX_DIR", str(ROOT.parent / "judgment_research_poc/index_50k"))).resolve()
CORPUS = DATA / "corpus"
DB = DATA / "workspace.sqlite3"
GATEWAY = os.getenv("CAIYAN_GATEWAY_URL", "http://127.0.0.1:8766")
SEARCH_URL = os.getenv("CAIYAN_SEARCH_URL", "http://127.0.0.1:8767")
CHAT_API_BASE = os.getenv("CAIYAN_CHAT_API_BASE", "http://127.0.0.1:8081/v1").rstrip("/")
EMBED_API_BASE = os.getenv("CAIYAN_EMBED_API_BASE", "http://127.0.0.1:8080/v1").rstrip("/")
CHAT_SERVER_MODEL = os.getenv("CAIYAN_CHAT_MODEL", "")
EMBED_SERVER_MODEL = os.getenv("CAIYAN_EMBED_MODEL", "")
MODEL = "qwen3:4b-instruct"
EMBED_MODEL = "qwen3-embedding:4b"
MODEL_ALIAS = "caiyan-qwen4b-r1"
EMBED_ALIAS = "caiyan-embedding4b-r1"
DIMENSIONS = 512
QUERY_INSTRUCTION = "Instruct: Retrieve Chinese judgments with legally comparable facts, evidence, and disputed issues for a lawyer's research question.\nQuery: "
WORKFLOW_VERSION = "caiyan-poc-1106-v1"
DEFAULT_CASE_ANALYSIS_COUNT = 3
EVIDENCE_VERSION = "evidence-filtered-v6"
EVIDENCE_MAX_CASES = 5
EVIDENCE_CONCURRENCY = 2
RESEARCH_VERSION = "research-top5-v1"
MAX_FILE = 20 * 1024 * 1024
MAX_CHARS = 60000
MAX_MATERIALS = 20

def internal_key():
    return (DATA / "internal.key").read_text().strip()
