"""Immutable, editable Word references generated from a saved evidence version."""
from io import BytesIO
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


def build_reference(case, artifact, batch, answers, created_at, timezone="UTC"):
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(.8)
    section.left_margin = section.right_margin = Inches(.85)
    for name in ("Normal", "Title", "Title Char", "Heading 1", "Heading 1 Char", "Heading 2", "Heading 2 Char", "List Number", "List Bullet"):
        style = document.styles[name]
        style.font.name = "Arial"
        fonts = style.element.get_or_add_rPr().rFonts
        for attribute in list(fonts.attrib):
            if "theme" in attribute.lower():
                del fonts.attrib[attribute]
        fonts.set(qn("w:eastAsia"), "Songti SC")
        style.font.color.rgb = RGBColor.from_string("17232F")
    for border in document.styles["Title"].element.xpath("./w:pPr/w:pBdr"):
        border.getparent().remove(border)
    document.styles["Title"].font.color.rgb = RGBColor(0, 0, 0)
    normal = document.styles["Normal"]
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.3
    normal.paragraph_format.space_after = Pt(7)
    document.styles["Title"].font.size = Pt(24)
    for name, size in (("Heading 1", 16), ("Heading 2", 12)):
        document.styles[name].font.size = Pt(size)
        document.styles[name].paragraph_format.space_before = Pt(15)
        document.styles[name].paragraph_format.space_after = Pt(8)
    view = artifact.get("evidence_view")
    if view is None:
        from .evidence_workspace import evidence_view
        view = evidence_view(artifact)
    document.add_heading("已有证据与材料", 1)
    for entry in view["existing_evidence"]:
        paragraph = document.add_paragraph(style="List Bullet")
        paragraph.add_run(entry["name"]).bold = True
        if entry.get("status"):
            paragraph.add_run(" · " + entry["status"])
    if not view["existing_evidence"]:
        document.add_paragraph("本轮未关联本案材料摘录，请核对分析范围。")
    document.add_heading("建议补充或核对的证据", 1)
    if view["additional_evidence"]:
        table = document.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        table.autofit = False
        for column, width in zip(table.columns, (2.3, 1.8, 2.7)):
            column.width = Inches(width)
        for cell, label in zip(table.rows[0].cells, ("证据", "获取渠道", "为何需要")):
            cell.paragraphs[0].add_run(label).bold = True
        table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for item in view["additional_evidence"]:
            cells = table.add_row().cells
            for cell, key in zip(cells, ("evidence_needed", "obtain_from", "why_needed")):
                cell.text = item.get(key) or "待核实"
        for row in table.rows:
            row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
            for cell, width in zip(row.cells, (2.3, 1.8, 2.7)):
                cell.width = Inches(width)
    else:
        document.add_paragraph("暂无建议补充或核对的证据。")
    document.add_heading("待核问题", 1)
    from .evidence_questions import question_details
    questions = question_details(view.get("follow_up_details", view.get("follow_up_questions", [])),
                                 (artifact.get("structured") or {}).get("items", []))
    for i, item in enumerate(questions, 1):
        paragraph = document.add_paragraph()
        paragraph.add_run(f"{i}. {item['question']}").bold = True
        paragraph.add_run("\n为何重要：" + item["why_matters"])
    if not questions:
        document.add_paragraph("暂无待核问题。")
    document.core_properties.title = "证据分析参考文档"
    document.core_properties.author = "裁研"
    output = BytesIO()
    document.save(output)
    return output.getvalue()
