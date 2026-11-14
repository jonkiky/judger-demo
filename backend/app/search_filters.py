"""The 18 source columns, immutable value identities, and shared hard predicates.

The sidecar indexes metadata only; the judgment and vector snapshots stay untouched.
"""
import hashlib
from dataclasses import dataclass
from datetime import date

SCHEMA_VERSION = 1
MISSING = "missing"

@dataclass(frozen=True)
class FilterField:
    key: str
    label: str
    kind: str = "category"
    stored: bool = True  # Present in the original documents table.

    @property
    def column(self):
        return ("d." if self.stored else "m.") + self.key


FIELDS = (
    FilterField("title", "标题", "text"),
    FilterField("case_number", "案号", "text"),
    FilterField("court", "法院"),
    FilterField("doc_type", "文书类型"),
    FilterField("case_type", "案件类型"),
    FilterField("procedure", "审理程序"),
    FilterField("judgment_date", "判决时间", "date"),
    FilterField("published_at", "发布时间", "date", False),
    FilterField("accepted_at", "受理时间", "date", False),
    FilterField("case_level", "案例级别", stored=False),
    FilterField("court_level", "法院级别", stored=False),
    FilterField("cause", "案由"),
    FilterField("province", "省"),
    FilterField("city", "市", stored=False),
    FilterField("judgment_year", "判决年份", stored=False),
    FilterField("outcome", "判决结果", "text", False),
    FilterField("laws", "法条", "text", False),
    FilterField("content", "文书内容", "text"),
)
FIELD_MAP = {f.key: f for f in FIELDS}
EXTRA_FIELDS = tuple(f for f in FIELDS if not f.stored)


def value_id(value):
    # NULL and the source CSV's empty string are the same explicit "未载明" choice.
    return MISSING if value is None or value == "" else hashlib.sha256(value.encode()).hexdigest()


def attach_filters(connection, corpus):
    connection.execute("ATTACH DATABASE ? AS facets", ((corpus / "filters.sqlite3").as_uri() + "?mode=ro",))


def predicates(req, *, exclude=None, include_query=False):
    """Bound parameters only. OR within a field, AND between fields.

    IN subqueries start with the indexed (field, value_id, document_id) keys and
    work identically for lexical retrieval, vector candidates and final resolution.
    """
    clauses, params = ["1=1"], []
    for field, values in sorted(req.filters.items()):
        if field != exclude and values:
            slots = ",".join("?" for _ in values)
            clauses.append(f"d.id IN (SELECT document_id FROM facets.document_filters WHERE field=? AND value_id IN ({slots}))")
            params.extend([field, *values])
    # Preserve the original API for saved workflow requests and external callers.
    for key in ("province", "court", "cause"):
        if key != exclude and (value := getattr(req, key, "")):
            clauses.append(f"d.{key}=?")
            params.append(value)
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        if value := getattr(req, key, None):
            clauses.append(f"d.judgment_date{op}?")
            params.append(value.isoformat() if isinstance(value, date) else value)
    if include_query:
        if req.mode == "case_number":
            clauses.append("d.case_number=?")
            params.append(req.query.strip())
        elif req.mode == "keyword":
            for word in req.query.split():
                clauses.append("instr(d.content,?)>0")
                params.append(word)
    return " AND ".join(clauses), params


def facet_response(connection, req, release):
    """A disjunctive facet: options are constrained by every OTHER field.

    Counts always count documents, not passages. Pagination has a stable order,
    and searching a long-text option checks the complete original field value.
    """
    c = connection
    where, params = predicates(req)
    matching = c.execute(f"SELECT count(*) FROM documents d WHERE {where}", params).fetchone()[0]
    catalog = {r["field"]: dict(r) for r in c.execute("SELECT * FROM facets.field_stats")}
    fields = [{"key": f.key, "label": f.label, "kind": f.kind,
               "distinct_values": catalog[f.key]["distinct_values"],
               "missing_documents": catalog[f.key]["missing_documents"]} for f in FIELDS]
    selected = {}
    for key, values in req.filters.items():
        if values:
            slots = ",".join("?" for _ in values)
            selected[key] = [dict(r) for r in c.execute(
                f"SELECT value_id,label,characters,is_missing FROM facets.filter_values WHERE field=? AND value_id IN ({slots}) ORDER BY label,value_id", [key, *values])]
    response = {"release_id": release["release_id"], "fields": fields,
                "matching_documents": matching, "selected": selected,
                "count_scope": "metadata_only", "logic": "OR within a field; AND across fields",
                "options": [], "total_options": 0, "has_more": False, "next_offset": None}
    if req.field is None:
        return response
    field = FIELD_MAP[req.field]
    where, params = predicates(req, exclude=req.field)
    if req.text.strip():
        where += f" AND (instr(lower(COALESCE({field.column},'')),lower(?))>0 OR (x.value_id=? AND instr(?,?)>0))"
        params.extend([req.text.strip(), MISSING, "未载明（空值）", req.text.strip()])
    base = f"""FROM facets.document_filters x JOIN documents d ON d.id=x.document_id
        JOIN facets.metadata m ON m.document_id=d.id
        WHERE x.field=? AND {where} GROUP BY x.value_id"""
    bindings = [req.field, *params]
    total = c.execute(f"SELECT count(*) FROM (SELECT x.value_id {base})", bindings).fetchone()[0]
    rows = c.execute(f"""SELECT v.value_id,v.label,v.characters,v.is_missing,g.count
        FROM (SELECT x.value_id,count(*) AS count {base}) g
        JOIN facets.filter_values v ON v.field=? AND v.value_id=g.value_id
        ORDER BY v.is_missing,v.label COLLATE NOCASE,v.value_id LIMIT ? OFFSET ?""",
        [*bindings, req.field, req.limit, req.offset]).fetchall()
    next_offset = req.offset + len(rows)
    response.update(options=[dict(r) for r in rows], total_options=total,
                    has_more=next_offset < total, next_offset=next_offset if next_offset < total else None)
    return response
