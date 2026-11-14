"""Snapshot completed index_50k with SQLite's online backup, never opening its Qdrant client."""
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import settings

def terms(text):
    result = []
    for token in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            result.extend(token[i:i+2] for i in range(len(token)-1))
            if len(token) == 1:
                result.append(token)
        else:
            result.append(token)
    return list(dict.fromkeys(result))

def backup(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(dest) as out:
        src.backup(out)
        assert out.execute("PRAGMA quick_check").fetchone()[0] == "ok"

def prepare():
    manifest = json.loads((settings.INDEX / "manifest.json").read_text())
    if manifest["embedding_model"] != settings.EMBED_MODEL or manifest["embedding_dimensions"] != settings.DIMENSIONS:
        raise RuntimeError("Source embedding profile does not match this POC")
    release = "index_50k-" + hashlib.sha256((settings.INDEX / "manifest.json").read_bytes()).hexdigest()[:16]
    if (settings.CORPUS / "release.json").exists():
        existing = json.loads((settings.CORPUS / "release.json").read_text())
        if existing["release_id"] != release:
            raise RuntimeError("Different release already installed; use a new CAIYAN_DATA_DIR")
        print("Corpus ready:", release, flush=True)
        return
    start = time.monotonic()
    staging = settings.DATA / "corpus.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    print("Creating consistent SQLite and Qdrant snapshots…", flush=True)
    backup(settings.INDEX / "judgments.sqlite3", staging / "judgments.sqlite3")
    backup(settings.INDEX / "qdrant/collection/judgment_passages/storage.sqlite", staging / "qdrant/collection/judgment_passages/storage.sqlite")
    shutil.copy2(settings.INDEX / "qdrant/meta.json", staging / "qdrant/meta.json")
    shutil.copy2(settings.INDEX / "manifest.json", staging / "manifest.json")
    with sqlite3.connect(staging / "judgments.sqlite3") as c, sqlite3.connect(staging / "lexical.sqlite3") as lexical:
        docs = c.execute("SELECT count(*) FROM documents").fetchone()[0]
        passages = c.execute("SELECT count(*) FROM passages").fetchone()[0]
        assert docs == manifest["indexed_judgments"] and passages == manifest["indexed_passages"]
        lexical.execute("CREATE VIRTUAL TABLE passages_fts USING fts5(passage_id UNINDEXED,doc_id UNINDEXED,tokens,tokenize='unicode61')")
        batch = []
        for row in c.execute("SELECT p.id,p.document_id,p.start,p.end,d.content,d.title,d.cause,d.sha256 FROM passages p JOIN documents d ON d.id=p.document_id"):
            pid, did, s, e, body, title, cause, sha = row
            if not (0 <= s < e <= len(body)):
                raise ValueError(f"Invalid source interval {pid}")
            batch.append((pid, did, " ".join(terms((title or "") + " " + (cause or "") + " " + body[s:e]))))
            if len(batch) >= 2000:
                lexical.executemany("INSERT INTO passages_fts VALUES (?,?,?)", batch)
                batch = []
        lexical.executemany("INSERT INTO passages_fts VALUES (?,?,?)", batch)
        lexical.execute("INSERT INTO passages_fts(passages_fts) VALUES ('optimize')")
        lexical.commit()
        assert lexical.execute("SELECT count(*) FROM passages_fts").fetchone()[0] == passages
        print("Verifying full document hashes…", flush=True)
        for did, body, expected in c.execute("SELECT id,content,sha256 FROM documents"):
            if hashlib.sha256(body.encode()).hexdigest() != expected:
                raise ValueError(f"Invalid document hash {did}")
    from qdrant_client import QdrantClient
    client = QdrantClient(path=str(staging / "qdrant"))
    qcount = client.count("judgment_passages", exact=True).count
    if qcount != passages:
        raise ValueError("Qdrant and SQLite counts differ")
    with sqlite3.connect(staging / "judgments.sqlite3") as c:
        expected = {r[0]: r[1] for r in c.execute("SELECT point_id,id FROM passages")}
    offset = None
    while True:
        points, offset = client.scroll("judgment_passages", limit=1000, offset=offset, with_payload=True, with_vectors=False)
        for point in points:
            if expected.pop(point.id, None) != (point.payload or {}).get("passage_id"):
                raise ValueError("Qdrant identity mismatch")
        if offset is None:
            break
    client.close()
    assert not expected
    report = {"release_id": release, "source_index": str(settings.INDEX), "documents": docs, "passages": passages,
              "qdrant_points": qcount, "embedding_model": settings.EMBED_MODEL, "dimensions": settings.DIMENSIONS,
              "scope": "2025 年 6 月来源 CSV 的 50,000 行跨案由样本；不是专门住房库，不代表现行法或全国裁判。",
              "tokenizer": "unicode-cjk-bigrams-v1", "validated_document_hashes": docs,
              "validated_intervals": passages, "validated_point_identities": qcount, "seconds": round(time.monotonic()-start, 2)}
    (staging / "release.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if settings.CORPUS.exists():
        raise RuntimeError("Unpublished corpus directory exists; inspect it before replacing")
    staging.rename(settings.CORPUS)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)

if __name__ == "__main__":
    prepare()
