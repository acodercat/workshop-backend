"""The meeting library: reading templates out of documents, and the rules
templates and vocabulary terms are held to."""

import io
import json
import zipfile

import pytest
from pydantic import ValidationError

from app.errors import (
    LibraryChangedError,
    TemplateImportTooLargeError,
    TemplateImportUnreadableError,
    TemplateImportUnsupportedError,
    VocabularyInvalidError,
)
from app.schemas.meeting_library import TemplateDraft, VocabularyTerm
from app.services.meeting import template_import
from app.services.meeting.template_import import import_template
from app.services.meeting.templates import builtin_draft, builtin_templates
from app.services.meeting.vocabulary import merge_terms

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(body: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    document = (
        f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _p(text: str, *, style: str = "", bold: bool = False) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p>{props}<w:r>{run_props}<w:t>{text}</w:t></w:r></w:p>"


def test_word_headings_become_sections_and_example_text_does_not() -> None:
    body = (
        _p("董事會會議", style="Title")
        + _p("出席者", style="Heading1")
        + _p("陳大文、李小明")  # example content, not structure
        + _p("寫作提示：列出姓名及職銜")
        + _p("討論事項", style="Heading1")
    )

    preview = import_template("board.docx", _docx(body))

    assert preview.draft.title == "board"
    assert preview.draft.sections == ["出席者", "討論事項"]
    guidance = preview.draft.options.section_guidance
    assert guidance is not None and guidance[0] == "列出姓名及職銜"
    assert "陳大文" not in json.dumps(preview.model_dump(), ensure_ascii=False)


def test_a_table_header_row_becomes_fields() -> None:
    row = lambda *cells: "<w:tr>" + "".join(f"<w:tc>{_p(c)}</w:tc>" for c in cells) + "</w:tr>"  # noqa: E731
    body = f"<w:tbl>{row('事項', '負責人', '期限')}{row('a', 'b', 'c')}</w:tbl>"

    preview = import_template("tasks.docx", _docx(body))

    assert preview.draft.sections == ["跟進事項"]
    assert preview.draft.options.table_fields == ["事項", "負責人", "期限"]


def test_a_dtd_is_refused_rather_than_expanded() -> None:
    hostile = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">]>'
        f'<w:document xmlns:w="{_W}"><w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p></w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", hostile)

    with pytest.raises(TemplateImportUnreadableError):
        import_template("x.docx", buffer.getvalue())


def test_a_zip_declaring_too_much_is_refused_before_unpacking() -> None:
    entries = {f"word/f{i}.xml": b"" for i in range(301)}

    with pytest.raises(TemplateImportTooLargeError):
        import_template("x.docx", _docx(_p("a"), extra=entries))


def test_the_committee_form_is_recognized_by_its_document_part(monkeypatch) -> None:
    document = _docx(_p("anything"))
    with zipfile.ZipFile(io.BytesIO(document)) as archive:
        import hashlib

        digest = hashlib.sha256(archive.read("word/document.xml")).hexdigest()
    monkeypatch.setattr(template_import, "_ERB_DOCUMENT_SHA256", digest)

    preview = import_template("renamed.docx", document)

    assert preview.draft == builtin_draft("public-erb-committee")


def test_text_headings_skip_dates_and_fences() -> None:
    text = "# 議程\n```\n## 不是標題\n```\n決議：\n2024年5月會議：\n- 列表：\n"

    preview = import_template("notes.txt", text.encode())

    assert preview.draft.sections == ["議程", "決議"]
    assert preview.uncertain  # the dated heading was dropped, and said so


def test_an_exported_markdown_template_round_trips() -> None:
    metadata = json.dumps({"title": "週會", "options": {"tone": "正式"}}, ensure_ascii=False)
    text = f"# 週會\n\n用途說明\n\n## 進度\n\n逐項列出\n\n## 風險\n\n```aioffice-meeting-template-v1\n{metadata}\n```\n"

    preview = import_template("weekly.md", text.encode())

    assert preview.draft.title == "週會"
    assert preview.draft.description == "用途說明"
    assert preview.draft.sections == ["進度", "風險"]
    assert preview.draft.options.tone == "正式"
    assert preview.draft.options.section_guidance == ["逐項列出", ""]


def test_a_truncated_export_is_refused_not_guessed() -> None:
    text = '# t\n\n## a\n\n```aioffice-meeting-template-v1\n{"title": "t"'

    with pytest.raises(TemplateImportUnreadableError):
        import_template("t.md", text.encode())


@pytest.mark.parametrize(
    ("name", "data", "error"),
    [
        ("slides.pdf", b"x", TemplateImportUnsupportedError),
        ("big.txt", b"x" * (2 * 1024 * 1024 + 1), TemplateImportTooLargeError),
        ("latin.txt", "café".encode("latin-1"), TemplateImportUnreadableError),
    ],
)
def test_unacceptable_uploads(name, data, error) -> None:
    with pytest.raises(error):
        import_template(name, data)


def test_every_builtin_template_is_a_valid_draft() -> None:
    for template in builtin_templates():
        assert builtin_draft(template["id"]) is not None


def test_guidance_must_cover_every_section() -> None:
    with pytest.raises(ValidationError):
        TemplateDraft(title="t", sections=["a", "b"], options={"section_guidance": ["only one"]})


def test_unknown_template_options_are_refused() -> None:
    with pytest.raises(ValidationError):
        TemplateDraft(title="t", sections=["a"], options={"colour": "red"})


@pytest.mark.parametrize("text", ["不", "x", "第3季", "No"])
def test_a_replacement_must_be_a_specific_misrecognition(text) -> None:
    with pytest.raises(ValidationError):
        VocabularyTerm(kind="replacement", text=text, replacement="fixed")


def _term(text: str, **kwargs) -> VocabularyTerm:
    return VocabularyTerm(kind=kwargs.pop("kind", "hotword"), text=text, **kwargs)


def test_the_same_spelling_twice_is_merged() -> None:
    merged = merge_terms([_term("HKGAI"), _term("HKGAI"), _term("Meeting")], categories=set())

    assert [key for key in merged] == [("hotword", "HKGAI"), ("hotword", "Meeting")]


def test_one_spelling_corrected_two_ways_is_refused() -> None:
    with pytest.raises(VocabularyInvalidError):
        merge_terms(
            [
                _term("港大", kind="replacement", replacement="HKU"),
                _term("港大", kind="replacement", replacement="CUHK"),
            ],
            categories=set(),
        )


def test_a_term_in_a_vanished_category_means_a_stale_editor() -> None:
    with pytest.raises(LibraryChangedError):
        merge_terms([_term("x", category="gone")], categories={"kept"})
