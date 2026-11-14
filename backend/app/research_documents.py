"""Editable Word snapshot of the displayed research report and its chat history."""
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo
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
    for grid in list(section._sectPr.findall(qn("w:docGrid"))):
        section._sectPr.remove(grid)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "List Bullet"):
        style = document.styles[name]
        style.font.name = "Arial"
        fonts = style.element.get_or_add_rPr().rFonts
        fonts.set(qn("w:eastAsia"), "Noto Sans CJK SC")
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn("w:"+attr), None)
        for border in list(style.element.xpath("./w:pPr/w:pBdr")):
            border.getparent().remove(border)
        style.font.color.rgb = RGBColor.from_string("17232F")
    normal = document.styles["Normal"]
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = Pt(16)
    normal.paragraph_format.space_after = Pt(7)
    document.styles["Title"].font.size = Pt(24)
    document.styles["Title"].font.color.rgb = RGBColor(0, 0, 0)
    for name, size in (("Heading 1", 16), ("Heading 2", 12)):
        document.styles[name].font.size = Pt(size)
        document.styles[name].paragraph_format.space_before = Pt(14)
        document.styles[name].paragraph_format.space_after = Pt(7)
    document.add_paragraph("类案研究与诉讼策略", "Title")
    document.add_paragraph(case["title"])
    stamp = datetime.fromisoformat(created_at).astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
    document.add_paragraph(f"生成时间：{stamp}\n研究版本：v{artifact['version']} / {artifact['id']}\n代理立场：{case['position']}")
    document.add_paragraph("供审阅与继续完善。类案仅供比较，不能证明本案事实；结论、材料原件及法源适用仍需核对。")
    # Export the actual visible version, including manual edits and chat refinements.
    for line in artifact["body"].splitlines():
        if not line.strip() or line.startswith("# "):
            continue
        if line.startswith("## "):
            document.add_heading(line[3:], 1)
        elif line.startswith("### "):
            document.add_heading(line[4:], 2)
        else:
            document.add_paragraph(line[2:] if line.startswith("- ") else line, "List Bullet" if line.startswith("- ") else None)
    data = artifact["structured"]
    reports = (data.get("multi_agent") or {}).get("case_reports", [])
    if reports:
        document.add_heading("逐案研究记录", 1)
        for report in reports:
            document.add_heading(report.get("title", report["doc_id"]), 2)
            document.add_paragraph(report.get("case_number", "")).paragraph_format.keep_with_next = True
            coverage = report.get("coverage", {})
            document.add_paragraph("分析范围："+("原文节选" if coverage.get("partial") else "本篇全文")).paragraph_format.keep_with_next = True
            for finding in report["findings"]:
                document.add_paragraph("争点："+finding["issue"]).paragraph_format.keep_with_next = True
                document.add_paragraph("事实："+finding["facts"]+"\n裁判理由："+finding["reasoning"]+"\n裁判结果："+finding["outcome"])
                document.add_paragraph(" ".join("["+sid+"]" for sid in finding["source_ids"]))
            for limit in report.get("limits", []):
                document.add_paragraph(limit)
    if data.get("conversation"):
        document.add_heading("研究追问与完善记录", 1)
        for i, turn in enumerate(data["conversation"], 1):
            document.add_heading(f"{i}. {turn['question']}", 2)
            document.add_paragraph(turn["answer"])
    document.add_heading("来源索引", 1)
    for source in artifact["sources"]:
        document.add_heading(f"[{source['id']}] {source['name']}", 2)
        document.add_paragraph(source.get("location", "")).paragraph_format.keep_with_next = True
        document.add_paragraph(source.get("quote", ""))
    footer = section.footer.paragraphs[0]
    footer.alignment = 2
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    document.core_properties.title = "类案研究与诉讼策略"
    document.core_properties.author = "裁研"
    output = BytesIO()
    document.save(output)
    return output.getvalue()
