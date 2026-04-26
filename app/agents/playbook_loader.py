"""Schema-aware playbook loader.

When a user uploads an extraction schema (via POST /api/v1/schemas), the
schema specifies which playbooks to run for that extraction. This module
materializes that into the dict shape the executor expects.

`load_playbooks_for_schema(schema_doc, builtin_playbooks)` returns:
    dict[field_id, Playbook]
containing only the playbooks the schema asks for — both references to
built-ins and inline custom playbooks.

If schema_doc is None, the full built-in BRD playbook set is returned
(backward-compatible default behavior).
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.playbooks.loader import _playbook_from_dict
from app.agents.playbooks.schema import Playbook

logger = logging.getLogger(__name__)


def load_playbooks_for_schema(
    schema_doc: dict[str, Any] | None,
    *,
    builtin_playbooks: dict[str, Playbook],
) -> dict[str, Playbook]:
    """Materialize a schema into a {field_id: Playbook} dict.

    Parameters
    ----------
    schema_doc
        Parsed JSON schema (validated by schema_validator before calling).
        If None, returns the full built-in playbook set.
    builtin_playbooks
        The 79 BRD playbooks (output of `get_playbooks()`).

    Returns
    -------
    Filtered playbook dict containing exactly the fields the schema requests.
    """
    if schema_doc is None:
        return dict(builtin_playbooks)

    fields_list = schema_doc.get("fields", [])
    if not isinstance(fields_list, list):
        logger.warning("schema has no 'fields' list — returning empty playbook set")
        return {}

    result: dict[str, Playbook] = {}

    for entry in fields_list:
        if not isinstance(entry, dict):
            continue

        # Reference to a built-in playbook
        if "use_playbook" in entry:
            pb_id = entry["use_playbook"]
            pb = builtin_playbooks.get(pb_id)
            if pb is None:
                # Validator should have caught this; log and skip
                logger.warning(
                    "schema references unknown built-in playbook '%s' — skipped",
                    pb_id,
                )
                continue
            result[pb_id] = pb
            continue

        # Inline custom playbook
        if "field_id" in entry:
            fid = entry["field_id"]
            try:
                pb = _materialize_inline(entry, builtin_playbooks)
            except Exception as exc:
                logger.error(
                    "failed to materialize inline field '%s': %s — skipped",
                    fid, exc,
                )
                continue
            result[fid] = pb

    logger.info(
        "schema='%s' v%s loaded: %d field(s) (%d built-in references, %d inline)",
        schema_doc.get("schema_id", "?"),
        schema_doc.get("version", "?"),
        len(result),
        sum(1 for f in fields_list if isinstance(f, dict) and "use_playbook" in f),
        sum(1 for f in fields_list if isinstance(f, dict) and "field_id" in f),
    )
    return result


def _materialize_inline(
    entry: dict[str, Any],
    builtin_playbooks: dict[str, Playbook],
) -> Playbook:
    """Convert an inline schema field-entry into a Playbook object.

    Inline entries follow the same JSON shape as `data/playbooks_compiled/*.json`,
    so we reuse the existing `_playbook_from_dict` deserializer.

    Defaults applied when the entry omits a field:
        property_applicability  -> {Retail, Industrial, Office, Mixed-Use: True}
        keywords                -> []
        summary_keywords        -> first 8 keywords
        amendment_controls      -> True
        depends_on              -> []
    """
    fid = entry["field_id"]

    # If override=True and a built-in exists, start from it and patch
    if entry.get("override", False) and fid in builtin_playbooks:
        # Caller wants to override a built-in. Take the entry as authoritative
        # but fall back to built-in fields where the entry is silent.
        builtin = builtin_playbooks[fid]
        merged: dict[str, Any] = {
            "field_id":              fid,
            "field_name":            entry.get("field_name", builtin.field_name),
            "category":              entry.get("category", builtin.category),
            "output_type":           entry.get("output_type", builtin.output_type),
            "property_applicability": entry.get(
                "property_applicability", builtin.property_applicability
            ),
            "abstract_applicability": entry.get(
                "abstract_applicability", builtin.abstract_applicability
            ),
            "source_docx":           entry.get("source_docx", builtin.source_docx),
            "overview":              entry.get("overview", builtin.overview),
            "preliminary":           entry.get("preliminary", builtin.preliminary),
            "questions":             entry.get(
                "questions",
                # Re-serialize built-in questions into dicts since
                # _playbook_from_dict expects them as dicts
                [_question_to_dict(q) for q in builtin.questions],
            ),
            "keywords":              entry.get("keywords", builtin.keywords),
            "summary_keywords":      entry.get("summary_keywords", builtin.summary_keywords),
            "amendment_controls":    entry.get("amendment_controls", builtin.amendment_controls),
            "depends_on":            entry.get("depends_on", builtin.depends_on),
        }
        if "few_shot_examples" in entry:
            merged["few_shot_examples"] = entry["few_shot_examples"]
        return _playbook_from_dict(merged)

    # Pure new playbook — apply minimal defaults
    enriched = dict(entry)
    enriched.setdefault("property_applicability", {
        "Retail": True, "Industrial": True, "Office": True, "Mixed-Use": True,
    })
    enriched.setdefault("abstract_applicability", {})
    enriched.setdefault("preliminary", [])
    enriched.setdefault("keywords", [])
    enriched.setdefault("summary_keywords", enriched["keywords"][:8])
    enriched.setdefault("amendment_controls", True)
    enriched.setdefault("depends_on", [])
    return _playbook_from_dict(enriched)


def _question_to_dict(q) -> dict:
    """Serialize a PlaybookQuestion back to dict shape for re-loading."""
    def _action_to_dict(a):
        if a is None:
            return None
        return {
            "type": a.type.value if hasattr(a.type, "value") else a.type,
            "goto": a.goto,
            "literal": a.literal,
            "also_extract": a.also_extract,
        }
    return {
        "id": q.id,
        "priority": q.priority,
        "condition_type": q.condition_type,
        "question_text": q.question_text,
        "extraction_hint": q.extraction_hint,
        "output_type": q.output_type,
        "search_scope": q.search_scope.value if hasattr(q.search_scope, "value") else q.search_scope,
        "keywords": q.keywords,
        "yes_branch": _action_to_dict(q.yes_branch),
        "no_branch":  _action_to_dict(q.no_branch),
        "red_flag":   q.red_flag,
        "notes":      q.notes,
    }
