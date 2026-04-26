"""Playbook loader.

Reads the JSON files emitted by the compiler into in-memory Playbook objects.
Cached for the process lifetime.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from app.agents.playbooks.schema import (
    ActionType, Playbook, PlaybookAction, PlaybookQuestion, SearchScope,
)

logger = logging.getLogger(__name__)


def _action_from_dict(d: dict | None) -> PlaybookAction | None:
    if d is None:
        return None
    return PlaybookAction(
        type=ActionType(d["type"]),
        goto=d.get("goto"),
        literal=d.get("literal"),
        also_extract=d.get("also_extract", False),
    )


def _playbook_from_dict(d: dict) -> Playbook:
    questions = [
        PlaybookQuestion(
            id=q["id"],
            priority=q["priority"],
            condition_type=q.get("condition_type", ""),
            question_text=q["question_text"],
            extraction_hint=q.get("extraction_hint"),
            output_type=q.get("output_type", "Text"),
            search_scope=SearchScope(q.get("search_scope", "all")),
            keywords=q.get("keywords", []),
            yes_branch=_action_from_dict(q.get("yes_branch")),
            no_branch=_action_from_dict(q.get("no_branch")),
            red_flag=q.get("red_flag"),
            notes=q.get("notes"),
        )
        for q in d.get("questions", [])
    ]
    return Playbook(
        field_id=d["field_id"],
        field_name=d["field_name"],
        category=d["category"],
        output_type=d.get("output_type", "Text"),
        property_applicability=d.get("property_applicability", {}),
        abstract_applicability=d.get("abstract_applicability", {}),
        source_docx=d.get("source_docx"),
        overview=d.get("overview"),
        preliminary=d.get("preliminary", []),
        questions=questions,
        keywords=d.get("keywords", []),
        summary_keywords=d.get("summary_keywords", []),
        amendment_controls=d.get("amendment_controls", True),
        depends_on=d.get("depends_on", []),
    )


@lru_cache(maxsize=1)
def get_playbooks() -> dict[str, Playbook]:
    """Load every compiled playbook JSON. Returns {field_id: Playbook}."""
    from app.config import settings
    out_dir: Path = Path(settings.brd_path).parent / "playbooks_compiled"
    if not out_dir.exists():
        logger.warning("No compiled playbooks at %s — run `python -m app.agents.playbooks.compiler`", out_dir)
        return {}

    result: dict[str, Playbook] = {}
    for path in out_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            with path.open() as fh:
                d = json.load(fh)
            pb = _playbook_from_dict(d)
            result[pb.field_id] = pb
        except Exception as exc:
            logger.warning("Failed to load playbook %s: %s", path, exc)
    logger.info("Loaded %d playbooks from %s", len(result), out_dir)
    return result


def get_playbook(field_id: str) -> Playbook | None:
    return get_playbooks().get(field_id)
