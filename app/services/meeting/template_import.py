"""Reading a summary template out of an uploaded document.

Three formats, each read for *structure only* — section names and the
writing guidance under them, never the example content, which would
otherwise be written into minutes as if it had been said:

- ``.docx`` — headings detected from styles, outline levels, numbering,
  short bold lines, and table header rows (:func:`_word_template`).
- ``.md`` — a template exported from this app round-trips exactly (a
  fenced metadata block carries what Markdown cannot); any other Markdown
  is read for its headings.
- ``.txt`` — lines that look like headings.

The heuristics are deliberately specific — each one matches a layout
that users' documents actually take — so a change to one changes how
existing documents import.

Documents are untrusted. DOCX is a zip of XML: the zip is bounded in entry
count and declared size before anything is decompressed, and the XML is
parsed with DTDs refused (no entity expansion) and with depth, element and
text-length limits.
"""

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any
from xml.parsers import expat

from pydantic import ValidationError

from app.errors import (
    TemplateImportTooLargeError,
    TemplateImportUnreadableError,
    TemplateImportUnsupportedError,
)
from app.schemas.meeting_library import (
    MAX_GUIDANCE_LENGTH,
    MAX_SECTIONS,
    MAX_TABLE_FIELDS,
    TemplateDraft,
    TemplatePreview,
)
from app.services.meeting.templates import builtin_draft

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
_MAX_ZIP_ENTRIES = 300
_MAX_UNZIPPED_BYTES = 10 * 1024 * 1024
_MAX_TEXT = 100_000
_MAX_DEPTH = 128
_MAX_ELEMENTS = 250_000
_DATE = re.compile(r"\d{4}[年/-]\d{1,2}[月/-]")
_FENCE = re.compile(r"^\s*(```|~~~)")

# The committee form this deployment's users file most. Its layout (visual
# headings, a parallel follow-up column) defeats the generic reader, so its
# main document part is recognized by hash and answered with the built-in
# template drawn from it — which survives renaming or repackaging the file,
# while an edited form is read on its own terms.
_ERB_DOCUMENT_SHA256 = "af044e3f9ea2b31edf7db7ea77d52f5c408bb1bbc21e9bdbfcee378c021a33e1"
_ERB_TEMPLATE_ID = "public-erb-committee"


def too_large() -> TemplateImportTooLargeError:
    """The refusal of a document over :data:`MAX_UPLOAD_BYTES`."""
    return TemplateImportTooLargeError(
        f"The document is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
    )


def import_template(filename: str, data: bytes) -> TemplatePreview:
    if not re.search(r"\.(docx|md|txt)$", filename, re.IGNORECASE) or len(filename) > 255:
        raise TemplateImportUnsupportedError("Only DOCX, MD and TXT documents can be imported.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise too_large()
    stem = re.sub(r"\.[^.]+$", "", filename)[:120]
    if filename.lower().endswith(".docx"):
        return _from_docx(stem, data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TemplateImportUnreadableError("Text documents must be UTF-8.") from None
    if filename.lower().endswith(".md"):
        exported = _read_exported_markdown(text)
        if exported is not None:
            return TemplatePreview(draft=exported, uncertain=[])
    return _from_text(stem, text)


def _from_text(title: str, text: str) -> TemplatePreview:
    if not title.strip() or not text.strip() or len(text) > _MAX_TEXT:
        raise TemplateImportUnreadableError("The document is empty or too long.")
    sections: list[str] = []
    # Insertion-ordered and deduplicated: one warning per kind of guess.
    uncertain: dict[str, None] = {}
    fenced = False
    for line in re.split(r"\r?\n", text):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        value = line.strip()
        markdown = re.match(r"^#{1,6}\s+(.+)$", value)
        if markdown:
            heading = markdown.group(1)
        elif len(value) <= 60 and not re.match(r"^[-*]", value) and re.search(r"[：:]$", value):
            heading = re.sub(r"[：:]$", "", value)
        else:
            heading = ""
        if _DATE.search(heading):
            uncertain["含具體日期的標題已排除，請核對章節。"] = None
            continue
        if heading and len(heading) <= 120 and heading not in sections:
            sections.append(heading)
    if not sections:
        uncertain["未識別出明確章節，請手動確認結構。"] = None
    return _preview(
        {
            "title": title,
            "description": "",
            "sections": sections[:MAX_SECTIONS] or ["會議概要"],
            "options": {"tone": "客觀簡潔", "missing": "未明確"},
        },
        list(uncertain),
    )


# The marker of a Markdown file this app exported. What follows it is JSON
# with the title and the options Markdown has no syntax for.
_EXPORT_MARKER = "```aioffice-meeting-template-v1\n"


def _read_exported_markdown(source: str) -> TemplateDraft | None:
    """The template in an exported Markdown file, or ``None`` when the file
    is not one (it is then read as ordinary Markdown)."""
    text = source.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    start = text.rfind("\n" + _EXPORT_MARKER)
    if start < 0:
        return None
    metadata = text[start + 1 + len(_EXPORT_MARKER) :].rstrip()
    invalid = TemplateImportUnreadableError(
        "The template file is incomplete; check its title and guidance."
    )
    if not metadata.endswith("\n```") or len(text) > _MAX_TEXT:
        raise invalid
    try:
        header = json.loads(metadata[:-4])
    except ValueError:
        raise invalid from None
    if (
        not isinstance(header, dict)
        or not isinstance(header.get("title"), str)
        or not isinstance(header.get("options"), dict)
    ):
        raise invalid
    draft = _parse_markdown_sections(text[:start], title=header["title"], options=header["options"])
    if draft is None:
        raise invalid
    return draft


def _parse_markdown_sections(
    source: str, *, title: str, options: dict[str, Any]
) -> TemplateDraft | None:
    """``# Title``, an optional description, then ``## Section`` headings
    each followed by its writing guidance."""
    sections: list[str] = []
    guidance: list[list[str]] = []
    before: list[str] = []
    has_title = False
    in_fence = False
    for line in source.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else re.match(r"^(#{1,2})(?:\s+|$)(.*?)\s*$", line)
        name = re.sub(r"\s+#+\s*$", "", heading.group(2)).strip() if heading else ""
        if heading and heading.group(1) == "#" and not sections and not has_title:
            title = name
            has_title = True
        elif heading and heading.group(1) == "##":
            sections.append(name)
            guidance.append([])
        elif sections:
            guidance[-1].append(line)
        else:
            before.append(line)
    description = (
        "\n".join(before).strip() if has_title or any(line.strip() for line in before) else ""
    )
    section_guidance = ["\n".join(lines).strip() for lines in guidance]
    if (
        not title.strip()
        or not sections
        or any(not section.strip() for section in sections)
        or len(sections) > MAX_SECTIONS
        or any(len(guidance_text) > MAX_GUIDANCE_LENGTH for guidance_text in section_guidance)
    ):
        return None
    try:
        return TemplateDraft.model_validate(
            {
                "title": title,
                "description": description,
                "sections": sections,
                "options": {**options, "section_guidance": section_guidance},
            }
        )
    except ValidationError:
        return None


def _preview(draft: dict[str, Any], uncertain: list[str]) -> TemplatePreview:
    try:
        return TemplatePreview(draft=TemplateDraft.model_validate(draft), uncertain=uncertain)
    except ValidationError:
        raise TemplateImportUnreadableError("No usable template structure was found.") from None


def _from_docx(title: str, data: bytes) -> TemplatePreview:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise TemplateImportUnreadableError("The DOCX file is malformed.") from None
    with archive:
        entries = archive.infolist()
        # Declared sizes bound what is read: ``zipfile`` stops at them.
        if (
            len(entries) > _MAX_ZIP_ENTRIES
            or sum(e.file_size for e in entries) > _MAX_UNZIPPED_BYTES
        ):
            raise TemplateImportTooLargeError("The DOCX file unpacks too large.")
        try:
            document = archive.read("word/document.xml")
            styles = (
                archive.read("word/styles.xml") if "word/styles.xml" in archive.namelist() else None
            )
        except KeyError:
            raise TemplateImportUnreadableError("The DOCX file has no document.") from None
        except (zipfile.BadZipFile, RuntimeError, ValueError):
            raise TemplateImportUnreadableError("The DOCX file is malformed.") from None
    for candidate in (document, document.removeprefix(b"\xef\xbb\xbf")):
        if hashlib.sha256(candidate).hexdigest() == _ERB_DOCUMENT_SHA256:
            draft = builtin_draft(_ERB_TEMPLATE_ID)
            if draft is not None:
                return TemplatePreview(draft=draft, uncertain=[])
    return _word_template(title, document, styles)


_WORD_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "http://purl.oclc.org/ooxml/wordprocessingml/main",
    }
)
_MARKUP_COMPATIBILITY = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_KEEP = frozenset(
    "body p pStyle outlineLvl r t tab br b tbl tr tc tblHeader style name basedOn".split()
)
# Content that is not part of the document as it reads: deleted or moved
# text, text boxes, revision history, and the fallback half of alternate
# content.
_SKIP = frozenset({"del", "moveFrom", "txbxContent", "pPrChange", "rPrChange"})


@dataclass
class _Node:
    tag: str
    val: str = ""
    style_id: str = ""
    text: str = ""
    children: list["_Node"] = field(default_factory=list)


class _TooComplex(Exception):
    pass


class _TooLong(Exception):
    pass


def _read_word_xml(xml: bytes) -> _Node:
    """The parts of a WordprocessingML document the template reader uses."""
    root = _Node("")
    parents = [root]
    retained: list[bool] = []
    skipped = 0
    count = 0
    length = 0

    def split(name: str) -> tuple[str, str]:
        uri, _, local = name.rpartition(" ")
        return uri, local

    def refuse_dtd(*_: Any) -> None:
        raise _TooComplex

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal skipped, count
        uri, local = split(name)
        word = uri in _WORD_NAMESPACES
        if (
            skipped
            or (word and local in _SKIP)
            or (uri == _MARKUP_COMPATIBILITY and local == "Fallback")
        ):
            skipped += 1
        count += 1
        if len(retained) >= _MAX_DEPTH or count > _MAX_ELEMENTS:
            raise _TooComplex
        keep = not skipped and word and local in _KEEP
        retained.append(keep)
        if not keep:
            return

        def attr(wanted: str) -> str:
            for key, value in attributes.items():
                attr_uri, attr_local = split(key)
                if attr_local == wanted and attr_uri in _WORD_NAMESPACES:
                    return value
            return ""

        node = _Node(local, val=attr("val"), style_id=attr("styleId"))
        parents[-1].children.append(node)
        parents.append(node)

    def characters(value: str) -> None:
        nonlocal length
        node = parents[-1]
        if not skipped and node.tag == "t":
            length += len(value)
            if length > _MAX_TEXT:
                raise _TooLong
            node.text += value

    def end(_: str) -> None:
        nonlocal skipped
        if retained.pop():
            parents.pop()
        if skipped:
            skipped -= 1

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.StartDoctypeDeclHandler = refuse_dtd
    parser.EntityDeclHandler = refuse_dtd
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.buffer_text = True
    try:
        parser.Parse(xml, True)
    except _TooLong:
        raise TemplateImportUnreadableError(
            "The document's text is too long for a template."
        ) from None
    except (_TooComplex, expat.ExpatError):
        raise TemplateImportUnreadableError("The DOCX XML is invalid or too complex.") from None
    return root


def _child(node: _Node, tag: str) -> _Node | None:
    return next((child for child in node.children if child.tag == tag), None)


def _content(node: _Node) -> str:
    if node.tag == "t":
        return node.text
    if node.tag in ("tab", "br"):
        return " "
    return (" " if node.tag == "tc" else "").join(_content(child) for child in node.children)


def _enabled(node: _Node | None) -> bool:
    return node is not None and not re.match(r"^(0|false|off|none)$", node.val, re.IGNORECASE)


def _is_heading_style(name: str) -> bool:
    return bool(re.match(r"^(?:heading|標題|标题)\s*[1-9]$", name, re.IGNORECASE))


def _has_outline(node: _Node) -> bool:
    level = _child(node, "outlineLvl")
    return bool(re.match(r"^[0-8]$", level.val if level else ""))


_COLUMN_NAME = re.compile(
    r"^(?:事項|事项|項目|项目|議題|议题|會議紀錄|会议记录|討論內容|讨论内容|跟進(?:事項)?|跟进(?:事项)?"
    r"|負責人|负责人|期限|截止日期|狀態|状态|備註|备注|行動|行动|task|action(?: item)?|follow[- ]?up"
    r"|owner|deadline|due date|status|notes)$",
    re.IGNORECASE,
)
_FIELD = re.compile(
    r"^(日期|時間|时间|地點|地点|主持人|記錄人|记录人|date|time|location|chair)\s*[：:]",
    re.IGNORECASE,
)
_BASIC_HEADING = re.compile(
    r"^(?:(?:會議|会议)?(?:基本資料|基本信息|基本資訊|資料|概要|背景|議程|议程|結束|结束)|出席者|列席者"
    r"|與會人員|与会人员|討論事項|讨论事项|決議|决议|行動事項|行动事项|其他事項|其他事项|簽署|签署|agenda"
    r"|attendees|discussion|decisions|action items|next steps|adjournment)$",
    re.IGNORECASE,
)
_TIP = re.compile(
    r"^(?:寫作提示|写作提示|整理要求|撰寫要求|writing guidance)\s*[：:]\s*(.+)$", re.IGNORECASE
)
_ORDINAL = re.compile(
    r"^(?:(?:議程|议程|議題|议题|agenda(?: item)?)\s*[（(]?[\d一二三四五六七八九十]+[)）.、：:\s]"
    r"|[一二三四五六七八九十]+、|\d{1,2}[.、)]\s+)",
    re.IGNORECASE,
)


def _guidance_for(name: str) -> str:
    if re.search(r"行動|行动|跟進|跟进|待辦|待办|action|follow.up", name, re.IGNORECASE):
        return "逐項列明已議定的行動、負責人及期限。只填寫會議中已確認的資料。"
    if re.search(r"決議|决议|決定|决定|結論|结论|decision", name, re.IGNORECASE):
        return "列出已確認的決定、適用條件及保留意見，區分建議與決議。"
    if re.search(r"出席|列席|與會|与会|attendees", name, re.IGNORECASE):
        return "依實際記錄列出人員、身分及參與方式。"
    if re.search(r"議程|议程|議題|议题|討論|讨论|agenda|discussion", name, re.IGNORECASE):
        return "按本議題整理討論重點、已確認的決定及後續安排。"
    if re.search(r"簽署|签署", name):
        return "保留簽署人及日期欄，供會後確認。"
    return "依會議記錄整理「" + name + "」的要點，只列有依據的內容。"


def _word_template(title: str, xml: bytes, styles_xml: bytes | None) -> TemplatePreview:
    body = _child(_read_word_xml(xml), "body")
    if body is None or not _content(body).strip():
        raise TemplateImportUnreadableError("The DOCX file has no readable text.")
    styles = {
        node.style_id: node
        for node in (_read_word_xml(styles_xml).children if styles_xml else [])
        if node.tag == "style"
    }

    def styled_heading(style_id: str) -> bool:
        seen: set[str] = set()
        while style_id and style_id not in seen:
            if _is_heading_style(style_id):
                return True
            seen.add(style_id)
            style = styles.get(style_id)
            if style is None:
                break
            name = _child(style, "name")
            if _has_outline(style) or _is_heading_style(name.val if name else ""):
                return True
            based_on = _child(style, "basedOn")
            style_id = based_on.val if based_on else ""
        return False

    # Insertion-ordered: dicts keep order, and so do the hint "sets".
    chapters: dict[str, dict[str, None]] = {}
    uncertain: dict[str, None] = {}
    fields: dict[str, None] = {}
    active: dict[str, None] | None = None

    def section(name: str) -> None:
        nonlocal active
        if name not in chapters and len(chapters) >= MAX_SECTIONS:
            active = None
            uncertain[f"僅保留首 {MAX_SECTIONS} 個章節，請核對範圍。"] = None
            return
        active = chapters.setdefault(name, {})

    def hint(text: str) -> None:
        if active is not None:
            active[text] = None

    def visit(node: _Node, in_cell: bool = False) -> None:
        if node.tag == "tbl":
            rows = [child for child in node.children if child.tag == "tr"]
            if not rows:
                return
            header = rows[0]
            labels = [
                re.sub(r"([㐀-鿿])\s+(?=[㐀-鿿])", r"\1", _content(cell).strip())
                for cell in header.children
                if cell.tag == "tc"
            ]
            named = [label for label in labels if label]
            header_row = (
                len(labels) >= 2
                and bool(named)
                and all(len(label) <= 40 for label in named)
                and (
                    _enabled(_child(header, "tblHeader"))
                    or all(_COLUMN_NAME.match(name) for name in named)
                )
            )
            if header_row:
                if not labels[0] and re.search(r"跟[進进]", labels[1]):
                    labels[0] = "會議紀錄"
                columns = [label for label in labels if label]
                for column in columns:
                    fields[column] = None
                if active is None and all(_COLUMN_NAME.match(column) for column in columns):
                    section(
                        "跟進事項"
                        if any(
                            re.search(r"跟[進进]|owner|負責人|负责人", column, re.IGNORECASE)
                            for column in columns
                        )
                        else "會議事項"
                    )
                hint("以「" + "」、「".join(columns) + "」分欄整理。")
            for row in rows[1 if header_row else 0 :]:
                cells = [child for child in row.children if child.tag == "tc"]
                key = re.sub(r"[：:]$", "", _content(cells[0] if cells else row).strip())
                if not header_row and len(cells) == 2 and _FIELD.match(key + "："):
                    if active is None:
                        section("會議資料")
                    hint("列出" + key + "。")
                    continue
                for cell in cells:
                    for part in cell.children:
                        visit(part, True)
            return
        if node.tag != "p":
            return
        text = re.sub(r"\s+", " ", _content(node)).strip()
        if not text:
            return
        tip = _TIP.match(text)
        if tip:
            hint(tip.group(1))
            return
        label = _FIELD.match(text)
        if label:
            if active is None:
                section("會議資料")
            hint("列出" + label.group(1) + "。")
            return
        if len(text) > 120 or _DATE.search(text):
            return
        style_node = _child(node, "pStyle")
        style = style_node.val if style_node else ""
        if re.match(r"^(?:title|subtitle)$", style, re.IGNORECASE):
            return
        markdown = re.match(r"^#{1,6}\s+(.+)$", text)
        named_heading = markdown.group(1) if markdown else None
        ordinal = bool(_ORDINAL.match(text))
        short = len(text) <= 60 and not re.search(r"[。！？!?；;]", text)
        runs = [child for child in node.children if child.tag == "r" and _content(child).strip()]
        bold = (
            not in_cell
            and short
            and not re.search(r"[：:]", text)
            and bool(runs)
            and all(_enabled(_child(run, "b")) for run in runs)
        )
        if bold and not _BASIC_HEADING.match(text):
            uncertain["部分章節從短粗體段落推斷，請核對標題。"] = None
        if (
            named_heading
            or styled_heading(style)
            or _has_outline(node)
            or _BASIC_HEADING.match(text)
            or (short and (ordinal or re.search(r"[：:]$", text)))
            or bold
        ):
            section(re.sub(r"[：:]$", "", named_heading or text).strip() or text)

    for node in body.children:
        visit(node)
    if not chapters:
        section("會議概要")
        uncertain["未識別出明確章節，請手動確認結構。"] = None
    sections = list(chapters)
    guidance = []
    for name in sections:
        hints = list(chapters[name])
        value = "\n".join(hints) if hints else _guidance_for(name)
        if len(value) > MAX_GUIDANCE_LENGTH:
            uncertain[f"部分章節提示超過 {MAX_GUIDANCE_LENGTH} 字，已截短，請核對。"] = None
        guidance.append(value[:MAX_GUIDANCE_LENGTH])
    if len(fields) > MAX_TABLE_FIELDS:
        uncertain[f"僅保留首 {MAX_TABLE_FIELDS} 個表格欄位。"] = None
    options: dict[str, Any] = {
        "tone": "客觀簡潔",
        "missing": "未明確",
        "section_guidance": guidance,
    }
    if fields:
        options["table_fields"] = list(fields)[:MAX_TABLE_FIELDS]
    return _preview(
        {"title": title, "description": "", "sections": sections, "options": options},
        list(uncertain),
    )
