"""Wire shapes for the meeting library: summary templates and vocabularies."""

import re
import unicodedata
import uuid
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

#
# Templates

_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class TemplateOptions(BaseModel):
    """How the model should write to a template. Strict: an unknown option
    is a client bug, not something to store and ignore."""

    model_config = ConfigDict(extra="forbid")

    tone: Annotated[str, StringConstraints(max_length=80)] | None = None
    missing: Annotated[str, StringConstraints(max_length=40)] | None = None
    instructions: Annotated[str, StringConstraints(max_length=2000)] | None = None
    output_format: Annotated[str, StringConstraints(max_length=40)] | None = None
    table_fields: (
        Annotated[list[Annotated[str, StringConstraints(max_length=80)]], Field(max_length=30)]
        | None
    ) = None
    section_guidance: (
        Annotated[list[Annotated[str, StringConstraints(max_length=1000)]], Field(max_length=20)]
        | None
    ) = None


class TemplateDraft(BaseModel):
    title: _Name
    description: Annotated[str, StringConstraints(max_length=500)] = ""
    sections: Annotated[list[_Name], Field(min_length=1, max_length=20)]
    options: TemplateOptions = TemplateOptions()

    @model_validator(mode="after")
    def _guidance_per_section(self) -> "TemplateDraft":
        guidance = self.options.section_guidance
        if guidance is not None and len(guidance) != len(self.sections):
            raise ValueError("section_guidance needs one entry per section")
        return self


class TemplateUpdate(TemplateDraft):
    # The version the edit was made against.
    expected_version: int = Field(ge=1)


class SummaryTemplate(TemplateDraft):
    id: str
    scope: Literal["personal", "public"]
    version: int
    can_delete: bool
    is_default: bool


class TemplateList(BaseModel):
    personal: list[SummaryTemplate]
    public: list[SummaryTemplate]


class DefaultTemplate(BaseModel):
    template_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class TemplatePreview(BaseModel):
    """A template read out of an uploaded document, for the user to review
    before saving. ``uncertain`` lists what the reader had to guess."""

    draft: TemplateDraft
    uncertain: list[str]
    # Always true: only structure is taken from the document, never its
    # example content, which would otherwise be written into minutes.
    sample_content_excluded: Literal[True] = True


#
# Vocabularies


_Spelling = Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]
_Reference = Annotated[str, StringConstraints(max_length=200)]
_NEGATIONS = re.compile(r"^(不|没|沒|无|無|非|未|否|not|no|never)$", re.IGNORECASE)


# Where a term was learned from. Written by the client (the vocabulary
# editor's import and transcript tools) in its own camelCase, stored as
# given and returned unchanged; nothing here interprets it, so it is only
# held to a shape and a size.


class TermEvidenceRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: Annotated[str, StringConstraints(max_length=1000)]


class TermEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meetingId: _Reference
    source: _Reference
    index: int = Field(ge=0)
    offsetKind: Literal["code-point"]
    ranges: Annotated[list[TermEvidenceRange], Field(max_length=20)]


class TermOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projectId: _Reference | None = None
    assetId: _Reference
    revisionId: _Reference
    entryId: _Reference
    side: Literal["eng", "chi"]


class VocabularyTerm(BaseModel):
    """A term as the client sends and receives it.

    ``id`` and ``version`` are the server's: a term is identified by its
    kind and spelling, so sending the same spelling again updates it, and a
    client-chosen id is ignored.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    version: int | None = None
    kind: Literal["hotword", "replacement"]
    text: Annotated[_Spelling, StringConstraints(min_length=1)]
    replacement: _Spelling = ""
    aliases: Annotated[
        list[Annotated[_Spelling, StringConstraints(min_length=1)]], Field(max_length=20)
    ] = []
    category: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)] | None = None
    enabled: bool = True
    evidence: TermEvidence | None = None
    origin: TermOrigin | None = None

    @model_validator(mode="after")
    def _a_replacement_is_specific(self) -> "VocabularyTerm":
        # A replacement fires wherever its text is recognized, so it must be
        # a specific misrecognition: two or more characters, no digits (a
        # number is never a misspelling of a word), and not a bare negation
        # (rewriting every "not" would invert sentences).
        if self.kind == "replacement" and (
            not self.replacement
            or len(self.text) < 2
            or any(unicodedata.category(char).startswith("N") for char in self.text)
            or _NEGATIONS.match(self.text)
        ):
            raise ValueError(
                "a replacement needs a specific misrecognition of two or more "
                "characters, without digits or negations, and a correction"
            )
        return self


class VocabularyCategory(BaseModel):
    id: str
    name: str


class Vocabulary(BaseModel):
    # ``"personal"`` or a project id.
    scope: str
    version: int
    terms: list[VocabularyTerm]
    categories: list[VocabularyCategory]


class VocabularySave(BaseModel):
    expected_version: int = Field(ge=0)
    terms: Annotated[list[VocabularyTerm], Field(max_length=300)]


_CategoryName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
]


class CategoryName(BaseModel):
    """Body for creating or renaming a category."""

    expected_version: int = Field(ge=0)
    name: _CategoryName


class VocabularyListEntry(BaseModel):
    scope: str
    # ``None`` for the personal vocabulary until the user names it; the
    # client gives it its localized name.
    name: str | None
    # The project's own name before any rename in this list; ``None`` for
    # the personal vocabulary.
    original_name: str | None
    hidden: bool


class VocabularyList(BaseModel):
    entries: list[VocabularyListEntry]
    version: int


class VocabularyListEntryUpdate(BaseModel):
    """Rename a vocabulary in the list, or hide / show it. Exactly one."""

    expected_list_version: int = Field(ge=0)
    name: _CategoryName | None = None
    hidden: bool | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "VocabularyListEntryUpdate":
        if (self.name is None) == (self.hidden is None):
            raise ValueError("give exactly one of name or hidden")
        return self
