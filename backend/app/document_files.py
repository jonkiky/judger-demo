"""Render saved document bodies as editable Word without web-framework dependencies."""
import io
import re
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from . import settings


def word_bytes(c, artifact):
    """Use exact edited body. Named {{slots}} retain Word paragraphs and tables."""
    template = artifact["structured"].get("template", {})
    document = Document()
    if template.get("source"):
        row = c.execute("SELECT * FROM materials WHERE id=? AND withdrawn=0", (template["id"],)).fetchone()
        if not row:
            raise ValueError("模板已撤回")
        if row["name"].lower().endswith(".docx"):
            document = Document(io.BytesIO((settings.DATA/"uploads"/row["object_key"]).read_bytes()))
    body = artifact["body"]
    # Edited Markdown is the authority, including headings and review notes.
    sections = {}
    heading = None
    for line in body.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip(); sections[heading] = []
        elif heading:
            sections[heading].append(line)
    slots = template.get("slots", [])
    filled_slots = False
    preamble = body.split("\n## ", 1)[0].strip()
    can_fill = (slots and set(slots).issubset(sections) and set(sections).issubset({*slots, "复核事项"})
                and preamble == "# "+artifact["title"])
    if can_fill and template.get("source"):
        paragraphs = list(document.paragraphs)
        def cells(tables):
            for table in tables:
                for row in table.rows:
                    for cell in row.cells:
                        yield from cell.paragraphs
                        yield from cells(cell.tables)
        paragraphs += list(cells(document.tables))
        seen = set()
        for paragraph in paragraphs:
            if "{{" not in paragraph.text:
                continue
            def replacement(match):
                key = match.group(1).strip()
                if key in sections:
                    seen.add(key)
                    return "\n".join(sections[key]).strip()
                return match.group(0)
            value = re.sub(r"\{\{\s*([^{}\n]+?)\s*\}\}", replacement, paragraph.text)
            # Preserve paragraph style and the first run's character formatting.
            if paragraph.runs:
                paragraph.runs[0].text = value
                for run in paragraph.runs[1:]: run.text = ""
            else:
                paragraph.add_run(value)
        filled_slots = set(slots).issubset(seen)
    if not filled_slots:
        # Keep uploaded styles, headers, footers and page settings; replace samples.
        for child in list(document.element.body):
            if child.tag != qn("w:sectPr"):
                document.element.body.remove(child)
        if not template.get("source") or not template["name"].lower().endswith(".docx"):
            section = document.sections[0]
            section.page_width, section.page_height = Inches(8.27), Inches(11.69)
            section.top_margin = section.bottom_margin = Inches(.8)
            for name in ("Normal", "Title", "Heading 1", "Heading 2", "List Bullet"):
                style = document.styles[name]
                style.font.name = "Arial"
                fonts = style.element.get_or_add_rPr().rFonts
                for attribute in list(fonts.attrib):
                    if "theme" in attribute.lower(): del fonts.attrib[attribute]
                fonts.set(qn("w:eastAsia"), "Songti SC")
                style.font.color.rgb = RGBColor(0, 0, 0)
                style.font.underline = False
                for border in style.element.xpath("./w:pPr/w:pBdr"):
                    border.getparent().remove(border)
            document.styles["Normal"].font.size = Pt(11)
            document.styles["Normal"].paragraph_format.line_spacing = 1.3
            document.styles["Title"].font.size = Pt(22)
        for line in body.splitlines():
            if not line.strip(): continue
            if line.startswith("# "): document.add_paragraph(line[2:], "Title")
            elif line.startswith("## "): document.add_heading(line[3:], 1)
            elif line.startswith("### "): document.add_heading(line[4:], 2)
            else: document.add_paragraph(line[2:] if line.startswith("- ") else line, "List Bullet" if line.startswith("- ") else None)
    elif sections.get("复核事项"):
        document.add_heading("复核事项", 1)
        document.add_paragraph("\n".join(sections["复核事项"]).strip())
    document.add_heading("来源与版本", 1)
    document.add_paragraph(f"文书草稿 v{artifact['version']}\n生成时间：{artifact['created_at']}\n模板：{template.get('name', '历史文书')}\n内容版本：{artifact['content_hash']}")
    for source in artifact["sources"]:
        document.add_heading(f"[{source['id']}] {source['name']}", 2)
        document.add_paragraph(f"{source.get('location', '')} · 材料版本 {source.get('source_version', '')}")
        if source.get("quote"): document.add_paragraph(source["quote"])
        if source.get("role") == "template": document.add_paragraph("文书模板，仅用于结构与格式。")
        if source["source_kind"] == "case_material":
            document.add_paragraph("原文：/api/v1/materials/"+source["doc_id"]+f" · 字符位置 {source.get('start', 0)}–{source.get('end', 0)}")
    document.core_properties.title = artifact["title"]
    document.core_properties.author = "裁研"
    output = io.BytesIO(); document.save(output)
    return output.getvalue()

