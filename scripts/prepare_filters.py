"""Recover all 18 CSV columns for the SAME 50k rows, without re-embedding.

Run automatically at startup. CSV hash, source row, every retained field and
full-text hash are checked before the sidecar is published atomically.
"""
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import settings
from backend.app.search_filters import FIELDS, EXTRA_FIELDS, SCHEMA_VERSION, value_id, MISSING


def source_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_filters(corpus, source_csv):
    start = time.monotonic()
    manifest = json.loads((corpus / "manifest.json").read_text())
    release = json.loads((corpus / "release.json").read_text())
    print("Verifying source CSV for 18-column filters…", flush=True)
    if source_hash(source_csv) != manifest["source_sha256"]:
        raise RuntimeError("Source CSV hash differs from index_50k; filter metadata was not published")
    saved_fields = tuple(f for f in FIELDS if f.stored and f.key != "content")
    source = sqlite3.connect((corpus / "judgments.sqlite3").as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    expected = {r["source_row"]: dict(r) for r in source.execute(
        "SELECT id,source_row,sha256," + ",".join(f.key for f in saved_fields) + " FROM documents")}
    source.close()
    total = len(expected)
    fd, tmp = tempfile.mkstemp(prefix="filters-", suffix=".staging", dir=corpus)
    os.close(fd)
    staging = Path(tmp)
    try:
        with sqlite3.connect(staging) as c:
            c.executescript("""
                CREATE TABLE filter_values(field TEXT NOT NULL,value_id TEXT NOT NULL,label TEXT NOT NULL,
                    characters INTEGER NOT NULL,is_missing INTEGER NOT NULL,PRIMARY KEY(field,value_id)) WITHOUT ROWID;
                CREATE TABLE document_filters(document_id TEXT NOT NULL,field TEXT NOT NULL,value_id TEXT NOT NULL,
                    PRIMARY KEY(document_id,field)) WITHOUT ROWID;
                CREATE TABLE field_stats(field TEXT PRIMARY KEY,distinct_values INTEGER,missing_documents INTEGER);
                CREATE TABLE filter_manifest(body TEXT NOT NULL);
            """)
            c.execute("CREATE TABLE metadata(document_id TEXT PRIMARY KEY," + ",".join(f.key + " TEXT" for f in EXTRA_FIELDS) + ")")
            csv.field_size_limit(sys.maxsize)
            checked = 0
            with source_csv.open(encoding="utf-8-sig", newline="") as f:
                rows = csv.DictReader(f)
                if rows.fieldnames != [field.label for field in FIELDS]:
                    raise RuntimeError("Unexpected CSV column layout")
                for source_row, row in enumerate(rows, 1):
                    if source_row not in expected:
                        continue
                    document = expected.pop(source_row)
                    if value_id(row["文书内容"]) != document["sha256"]:
                        raise RuntimeError(f"Full-text hash mismatch at source row {source_row}")
                    for field in saved_fields:
                        if row[field.label] != document[field.key]:
                            raise RuntimeError(f"Metadata mismatch at source row {source_row}: {field.label}")
                    did = document["id"]
                    c.execute("INSERT INTO metadata VALUES (" + ",".join("?" for _ in range(len(EXTRA_FIELDS)+1)) + ")",
                              [did, *(row[field.label] for field in EXTRA_FIELDS)])
                    values, links = [], []
                    for field in FIELDS:
                        value = row[field.label]
                        key = value_id(value)
                        # Keep original values in the original table / extra metadata table;
                        # the dropdown dictionary contains only bounded previews, never duplicate full texts.
                        label = " ".join(value.split()) if value else "未载明（空值）"
                        if len(label) > 180:
                            label = label[:180] + "…"
                        if field.key == "content":
                            label = row["案号"] + " · " + label
                        values.append((field.key, key, label, len(value), int(key == MISSING)))
                        links.append((did, field.key, key))
                    c.executemany("INSERT OR IGNORE INTO filter_values VALUES (?,?,?,?,?)", values)
                    c.executemany("INSERT INTO document_filters VALUES (?,?,?)", links)
                    checked += 1
                    if checked % 10000 == 0:
                        print(f"Filter metadata: {checked:,}/{total:,} documents", flush=True)
            if expected:
                raise RuntimeError("Some indexed source rows were not found in CSV")
            c.execute("CREATE INDEX filter_lookup ON document_filters(field,value_id,document_id)")
            c.execute("CREATE INDEX value_labels ON filter_values(field,is_missing,label COLLATE NOCASE,value_id)")
            for field in FIELDS:
                c.execute("INSERT INTO field_stats VALUES (?,?,?)", (field.key,
                    c.execute("SELECT count(*) FROM filter_values WHERE field=?", (field.key,)).fetchone()[0],
                    c.execute("SELECT count(*) FROM document_filters WHERE field=? AND value_id=?", (field.key, MISSING)).fetchone()[0]))
            report = {"schema_version": SCHEMA_VERSION, "release_id": release["release_id"],
                      "source_sha256": manifest["source_sha256"], "documents": checked,
                      "fields": len(FIELDS), "associations": checked * len(FIELDS),
                      "verified_source_rows": checked, "verified_full_text_hashes": checked,
                      "verified_retained_fields": [field.label for field in saved_fields],
                      "seconds": round(time.monotonic() - start, 2)}
            c.execute("INSERT INTO filter_manifest VALUES (?)", (json.dumps(report, ensure_ascii=False),))
            c.execute("ANALYZE")
            assert c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        staging.replace(corpus / "filters.sqlite3")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return report
    finally:
        staging.unlink(missing_ok=True)


def prepare():
    with (settings.CORPUS / ".filters-build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        dest = settings.CORPUS / "filters.sqlite3"
        release = json.loads((settings.CORPUS / "release.json").read_text())
        if dest.exists():
            with sqlite3.connect(dest.as_uri() + "?mode=ro", uri=True) as c:
                report = json.loads(c.execute("SELECT body FROM filter_manifest").fetchone()[0])
            if report["release_id"] != release["release_id"]:
                raise RuntimeError("Filter sidecar belongs to a different release")
            if report["schema_version"] == SCHEMA_VERSION:
                print("18-column filters ready:", report["documents"], flush=True)
                return report
        manifest = json.loads((settings.CORPUS / "manifest.json").read_text())
        source_csv = Path(os.getenv("CAIYAN_SOURCE_CSV", manifest["source_csv"]))
        return build_filters(settings.CORPUS, source_csv)


if __name__ == "__main__":
    prepare()
