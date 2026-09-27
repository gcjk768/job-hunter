# -*- coding: utf-8 -*-
"""Regenerates every resume and cover letter .docx from build/content.py.

    python build/build_docs.py

Output:
    master/James_Koh_Resume_Master.docx
    tailored/<key>/James_Koh_Resume_<Company>.docx
    tailored/<key>/James_Koh_Cover_Letter_<Company>.docx

Styling is deliberately ATS-plain: single column, no tables, no text boxes,
no headers/footers, standard fonts, real bullet lists.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Inches, Emu

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # content.py is the real resume and stays local; the public repo ships content_example.py
    from content import CONTACT, MASTER, VARIANTS  # noqa: E402
except ImportError:
    from content_example import CONTACT, MASTER, VARIANTS  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = "Calibri"
INK = RGBColor(0x1A, 0x1A, 0x1A)
MUTED = RGBColor(0x44, 0x44, 0x44)


# ----------------------------------------------------------------- helpers --

def _new_doc() -> Document:
    doc = Document()
    for section in doc.sections:
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(0.65)
        section.right_margin = Inches(0.65)
    style = doc.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(10)
    style.font.color.rgb = INK
    style.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    pf = style.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = 1.06
    return doc


def _para(doc, text="", size=10, bold=False, italic=False, align=None,
          space_before=0, space_after=0, color=None, style=None):
    p = doc.add_paragraph(style=style)
    pf = p.paragraph_format
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)
    if align is not None:
        p.alignment = align
    if text:
        run = p.add_run(text)
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = color or INK
    return p


def _rule(paragraph):
    """Thin bottom border - used under section headings."""
    pPr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "8A8A8A")
    borders.append(bottom)
    pPr.append(borders)


def _heading(doc, text):
    p = _para(doc, text.upper(), size=10.5, bold=True, space_before=9, space_after=3)
    p.runs[0].font.color.rgb = RGBColor(0x00, 0x33, 0x66)
    _rule(p)
    return p


def _bullet(doc, text, size=10):
    p = doc.add_paragraph(style="List Bullet")
    pf = p.paragraph_format
    pf.space_before = Pt(1.5)
    pf.space_after = Pt(1.5)
    pf.left_indent = Inches(0.22)
    pf.first_line_indent = Inches(-0.14)
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.name = FONT
    run.font.color.rgb = INK
    return p


def _labelled(doc, label, body, size=10, space_before=2):
    """'Label — body' on one wrapped paragraph."""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(1)
    pf.left_indent = Inches(0.0)
    r1 = p.add_run(label)
    r1.font.bold = True
    r1.font.size = Pt(size)
    r2 = p.add_run("  " + body)
    r2.font.size = Pt(size)
    r2.font.color.rgb = INK
    return p


def _tab_right(paragraph, doc):
    """Add a right-aligned tab stop at the right margin."""
    section = doc.sections[0]
    width = Emu(section.page_width - section.left_margin - section.right_margin)
    pPr = paragraph._p.get_or_add_pPr()
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "right")
    tab.set(qn("w:pos"), str(int(width.twips)))
    tabs.append(tab)
    pPr.append(tabs)


def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


# ------------------------------------------------------------------ resume --

def build_resume(spec, out_path):
    doc = _new_doc()

    # Header
    p = _para(doc, CONTACT["name"], size=20, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    p.runs[0].font.color.rgb = RGBColor(0x00, 0x33, 0x66)
    _para(doc, spec["tagline"], size=10, bold=True,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_before=2, color=MUTED)
    contact_line = "  |  ".join([
        CONTACT["location"], CONTACT["phone"], CONTACT["email"], CONTACT["linkedin"],
        CONTACT["github"],
    ])
    _para(doc, contact_line, size=9.5, align=WD_ALIGN_PARAGRAPH.CENTER,
          space_before=2, space_after=2, color=MUTED)

    _heading(doc, "Professional Summary")
    _para(doc, spec["summary"], size=10, space_before=1)

    _heading(doc, "Core Skills")
    for label, body in spec["skills"]:
        _labelled(doc, label + ":", body, size=9.5)

    _heading(doc, "Professional Experience")
    for job in spec["experience"]:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(3)
        p.paragraph_format.space_after = Pt(0)
        _tab_right(p, doc)
        r = p.add_run("%s — %s" % (job["title"], job["company"]))
        r.font.bold = True
        r.font.size = Pt(11)
        r2 = p.add_run("\t" + job["dates"])
        r2.font.size = Pt(9.5)
        r2.font.bold = True
        r2.font.color.rgb = MUTED
        _para(doc, job["subhead"], size=9.5, italic=True, space_after=2, color=MUTED)
        for b in job["bullets"]:
            _bullet(doc, b)

    _heading(doc, "Selected Engineering Projects")
    for proj in spec["projects"]:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(3)
        p.paragraph_format.space_after = Pt(0)
        r = p.add_run(proj["name"])
        r.font.bold = True
        r.font.size = Pt(10)
        r2 = p.add_run("  (%s)" % proj["context"])
        r2.font.size = Pt(9)
        r2.font.italic = True
        r2.font.color.rgb = MUTED
        _para(doc, proj["text"], size=9.5, space_after=0)
        p3 = _para(doc, "", size=9, space_after=1)
        rr = p3.add_run("Stack: ")
        rr.font.bold = True
        rr.font.size = Pt(9)
        rr.font.color.rgb = MUTED
        rr2 = p3.add_run(proj["stack"])
        rr2.font.size = Pt(9)
        rr2.font.color.rgb = MUTED

    _heading(doc, "Certifications")
    for c in spec["certifications"]:
        _bullet(doc, c, size=9.5)

    _heading(doc, "Education")
    for title, detail in spec["education"]:
        _para(doc, title, size=10, bold=True, space_before=3)
        _para(doc, detail, size=9.5, color=MUTED)
    _para(doc, spec["extra"], size=9, space_before=4, color=MUTED)

    doc.save(out_path)
    return out_path


# ------------------------------------------------------------ cover letter --

def build_cover_letter(variant, out_path):
    doc = _new_doc()

    p = _para(doc, CONTACT["name"], size=18, bold=True)
    p.runs[0].font.color.rgb = RGBColor(0x00, 0x33, 0x66)
    _para(doc, "  |  ".join([CONTACT["location"], CONTACT["phone"],
                             CONTACT["email"], CONTACT["linkedin"], CONTACT["github"]]),
          size=9.5, space_before=2, space_after=6, color=MUTED)

    _para(doc, _dt.date.today().strftime("%d %B %Y"), size=10, space_after=8, color=MUTED)

    _para(doc, "Hiring Team, %s" % variant["company"], size=10, bold=True)
    _para(doc, "Re: %s" % variant["role"], size=10, bold=True, space_after=8)

    _para(doc, "Dear Hiring Team,", size=10, space_after=6)
    for para_text in variant["letter"]:
        _para(doc, para_text, size=10, space_after=6)

    _para(doc, "Thank you for your time and consideration. I would welcome the chance to talk through any "
               "of the above in more detail.", size=10, space_before=2, space_after=10)
    _para(doc, "Warm regards,", size=10, space_after=2)
    _para(doc, CONTACT["name"], size=10, bold=True)

    doc.save(out_path)
    return out_path


# -------------------------------------------------------------------- main --

def main():
    built = []

    os.makedirs(os.path.join(ROOT, "master"), exist_ok=True)
    built.append(build_resume(MASTER, os.path.join(
        ROOT, "master", "James_Koh_Resume_Master.docx")))

    for v in VARIANTS:
        spec = dict(MASTER)
        spec["tagline"] = v["tagline"]
        spec["summary"] = v["summary"]
        # Optional per-variant overrides. Most roles read the master blocks; a
        # variant only overrides when the ordering itself is the argument
        # (e.g. leading with the games degree for a games-industry role).
        for key in ("skills", "education", "experience"):
            if key in v:
                spec[key] = v[key]
        wanted = v["projects"]
        by_name = {p["name"]: p for p in MASTER["projects"]}
        missing = [n for n in wanted if n not in by_name]
        if missing:
            raise SystemExit("Unknown project name(s) in %s: %s" % (v["key"], missing))
        spec["projects"] = [by_name[n] for n in wanted]

        outdir = os.path.join(ROOT, "tailored", v["key"])
        os.makedirs(outdir, exist_ok=True)
        co = _slug(v["company"])
        built.append(build_resume(spec, os.path.join(
            outdir, "James_Koh_Resume_%s.docx" % co)))
        built.append(build_cover_letter(v, os.path.join(
            outdir, "James_Koh_Cover_Letter_%s.docx" % co)))

    for path in built:
        print("built  %s  (%d KB)" % (os.path.relpath(path, ROOT),
                                      round(os.path.getsize(path) / 1024)))
    print("\n%d files built." % len(built))


if __name__ == "__main__":
    main()
