"""Build editable timeline versions without changing source quotations."""
from .contracts import TimelineFactInput
from .materials import material_document_ref


def edited_timeline(c, artifact: dict, inputs: list[TimelineFactInput]):
    sources = {s["id"]: s for s in artifact["sources"] if s["source_kind"] == "case_material"}
    saved_sources = list(artifact["sources"])
    originals = {f["id"]: f for f in artifact["structured"].get("facts", [])}
    facts, seen = [], set()
    for item in inputs:
        if item.id in seen:
            raise ValueError("时间线事件编号不能重复")
        seen.add(item.id)
        source = sources.get(item.source_ref)
        if item.source_ref and not source:
            raise ValueError("原材料引用已变化，请重新选择材料中心中的材料")
        material_id = item.material_id or (source and source["doc_id"])
        material = c.execute("SELECT * FROM materials WHERE id=? AND case_id=? AND active=1 AND withdrawn=0",
                             (material_id, artifact["case_id"])).fetchone()
        if not material or (source and source["doc_id"] != material_id):
            raise ValueError("请选择本案材料中心中的有效材料，不能使用其他案件或已撤回、已替换的材料")
        if not source:
            source = material_document_ref(material)
            if source["id"] not in sources:
                sources[source["id"]] = source
                saved_sources.append(source)
        values = item.model_dump()
        values.update(source_ref=source["id"], material_id=material_id)
        for key in ("date_text", "kind", "description"):
            values[key] = values[key].strip()
        if not values["kind"] or not values["description"]:
            raise ValueError("事件类型和事件内容不能为空")
        original = originals.get(item.id, {})
        changed = not original or any(
            values[key] != original.get(key, original.get("quote", "") if key == "description" else "")
            for key in ("date_text", "kind", "description", "source_ref")
        )
        edited = changed or original.get("edited", False)
        facts.append({**original, **values, "quote": source["quote"], "edited": edited,
                      "status": "人工整理 · 待律师核实" if edited else original.get("status", "材料记载 · 待律师核实")})
    structured = {**artifact["structured"], "facts": facts}
    sections = ["# 案情与时间线", "以下事件包含人工整理，材料原文保留供核对；尚未确认真实性，不等于法院认定。"]
    for fact in facts:
        sections.append(f"- **{fact['date_text'] or '日期未载明'} · {fact['kind']}**：{fact['description']} [{fact['source_ref']}]\n\n  {fact['status']}")
        if fact["quote"] and fact["description"] != fact["quote"]:
            sections.append("  材料原文：" + fact["quote"])
        elif not fact["quote"]:
            sections.append("  关联材料：" + sources[fact["source_ref"]]["name"] + "（未指定引用片段，待核对）")
    if structured.get("conflicts"):
        sections.append("## 核对提示\n\n" + structured["conflicts"])
    if structured.get("unresolved"):
        sections.append("## 待核事项\n\n" + "\n".join("- " + item for item in structured["unresolved"]))
    body = "\n\n".join(sections)
    if len(body) > 50000:
        raise ValueError("时间线内容过长，请精简至 50,000 字以内")
    structured["edited_body"] = body
    return body, structured, saved_sources
