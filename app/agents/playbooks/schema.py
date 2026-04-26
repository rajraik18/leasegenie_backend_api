"""Playbook schema.

A Playbook encodes the decision-tree a specialist agent follows to extract
one lease field. Compiled from the BRD's .docx guides + Questions.xlsx.

Design:
    Playbook
      └── sections: ordered list of PlaybookQuestion
              ├── id: "Q1", "Q2", ...
              ├── priority
              ├── condition_type (from Questions.xlsx)
              ├── question_text
              ├── extraction_hint ("Extract Amount" / "Record None" / "Extract Clause"...)
              ├── output_type  (Currency | Text | Date | Number | Numeric/Text)
              ├── yes_branch   — PlaybookAction to run when LLM answers YES
              ├── no_branch    — PlaybookAction to run when LLM answers NO
              ├── search_scope — Summary | Definitions | Body | All | Amendments
              └── red_flag     — red-flag pattern tied to this question
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class SearchScope(str, Enum):
    SUMMARY = "summary"
    DEFINITIONS = "definitions"
    BODY = "body"
    AMENDMENTS = "amendments"
    ALL = "all"


class ActionType(str, Enum):
    GOTO = "goto"              # jump to another question id
    EXTRACT = "extract"        # extract value, possibly then goto
    FINALIZE = "finalize"      # terminate with the extracted value
    FLAG_REVIEW = "flag"       # emit for manual review
    RECORD_NONE = "none"       # record "None" per BRD never-blank rule
    RECORD_LITERAL = "literal" # record a specific literal (e.g. "No Recovery", "undated")


@dataclass
class PlaybookAction:
    """What to do after a YES/NO answer."""
    type: ActionType
    goto: str | None = None                # next question id when type == GOTO / EXTRACT
    literal: str | None = None             # value to record when type == RECORD_LITERAL
    also_extract: bool = False             # EXTRACT + then goto

    @classmethod
    def goto_q(cls, qid: str) -> "PlaybookAction":
        return cls(type=ActionType.GOTO, goto=qid)

    @classmethod
    def extract_then(cls, qid: str | None = None) -> "PlaybookAction":
        return cls(type=ActionType.EXTRACT, goto=qid, also_extract=True)

    @classmethod
    def finalize(cls) -> "PlaybookAction":
        return cls(type=ActionType.FINALIZE)

    @classmethod
    def flag(cls) -> "PlaybookAction":
        return cls(type=ActionType.FLAG_REVIEW)

    @classmethod
    def record_none(cls) -> "PlaybookAction":
        return cls(type=ActionType.RECORD_NONE)

    @classmethod
    def record(cls, literal: str) -> "PlaybookAction":
        return cls(type=ActionType.RECORD_LITERAL, literal=literal)


@dataclass
class PlaybookQuestion:
    id: str                                # "Q1", "Q2", ...
    priority: int
    condition_type: str                    # from Questions.xlsx "Condition Type"
    question_text: str
    extraction_hint: str | None            # "Extract Amount" / "Extract Clause" / ...
    output_type: str                       # "Currency" | "Text" | "Date" | "Number" | "Numeric/Text"
    search_scope: SearchScope = SearchScope.ALL
    keywords: list[str] = field(default_factory=list)
    yes_branch: PlaybookAction | None = None
    no_branch: PlaybookAction | None = None
    red_flag: str | None = None
    notes: str | None = None


@dataclass
class Playbook:
    field_id: str                           # slug matching the Field List
    field_name: str                         # "Annual Base Rent"
    category: str                           # "Financial Clauses" | "Basic Information" | ...
    output_type: str                        # declared canonical output
    property_applicability: dict[str, bool] = field(default_factory=dict)
    abstract_applicability: dict[str, bool] = field(default_factory=dict)
    source_docx: str | None = None          # which .docx this came from
    overview: str | None = None             # the "General info" intro text
    preliminary: list[str] = field(default_factory=list)  # preliminary instructions
    questions: list[PlaybookQuestion] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    summary_keywords: list[str] = field(default_factory=list)
    amendment_controls: bool = True         # "If conflict exists → Amendment controls"
    depends_on: list[str] = field(default_factory=list)  # field_ids this one needs first
    few_shot_examples: list[dict] = field(default_factory=list)  # optional per-playbook examples

    def question(self, qid: str) -> PlaybookQuestion | None:
        for q in self.questions:
            if q.id == qid:
                return q
        return None

    def first_question(self) -> PlaybookQuestion | None:
        return self.questions[0] if self.questions else None
