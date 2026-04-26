"""Validates uploaded extraction-schema JSON before persistence.

Catches structural and semantic errors up-front so users get actionable
400 responses instead of mysterious extraction failures hours later.

Public API:
    validate_extraction_schema(json_doc, available_playbook_ids) -> ValidationResult
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Allowed value vocabularies
# ---------------------------------------------------------------------------

VALID_OUTPUT_TYPES = {
    "Text", "Currency", "Number", "Numeric", "Amount", "Money",
    "Date", "Percentage", "Percent", "Integer", "Yes/No", "Boolean",
    "List", "Derived",
}
VALID_SEARCH_SCOPES = {"all", "summary", "definitions", "amendments"}
VALID_PROPERTY_TYPES = {"Retail", "Industrial", "Office", "Mixed-Use"}
VALID_CONDITION_TYPES = {
    "Definition Based", "Amount Based", "Period Based", "Date Based",
    "Number Based", "Percent Based", "Percentage Based", "Yes/No",
    "Location Based",
}
VALID_BRANCH_TYPES = {"extract", "literal", "goto", "none"}

SCHEMA_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
FIELD_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,127}$")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ValidationError:
    path: str            # e.g. "fields[2].questions[0].output_type"
    code: str            # e.g. "INVALID_OUTPUT_TYPE"
    message: str         # human-readable

    def to_dict(self) -> dict:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    field_count: int = 0
    references_count: int = 0      # count of `use_playbook` references
    inline_count: int = 0          # count of inline custom fields

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors":   [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "field_count": self.field_count,
            "references_count": self.references_count,
            "inline_count": self.inline_count,
        }


# ---------------------------------------------------------------------------
# Top-level validator
# ---------------------------------------------------------------------------

def validate_extraction_schema(
    doc: dict,
    *,
    available_playbook_ids: set[str],
) -> ValidationResult:
    """Validate a parsed JSON schema. Never raises — returns errors instead."""
    result = ValidationResult(ok=False)

    if not isinstance(doc, dict):
        result.errors.append(ValidationError(
            path="$", code="NOT_OBJECT",
            message="schema must be a JSON object at the top level",
        ))
        return result

    # ---- top-level required fields ----
    schema_id = doc.get("schema_id")
    if not isinstance(schema_id, str) or not schema_id:
        result.errors.append(ValidationError(
            path="$.schema_id", code="MISSING",
            message="'schema_id' is required (slug, lowercase, 3-128 chars, [a-z0-9_-])",
        ))
    elif not SCHEMA_ID_PATTERN.match(schema_id):
        result.errors.append(ValidationError(
            path="$.schema_id", code="INVALID_FORMAT",
            message="schema_id must match [a-z][a-z0-9_-]{2,127}",
        ))

    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        result.errors.append(ValidationError(
            path="$.name", code="MISSING",
            message="'name' is required (display name, non-empty string)",
        ))

    version = doc.get("version", "1.0.0")
    if not VERSION_PATTERN.match(str(version)):
        result.errors.append(ValidationError(
            path="$.version", code="INVALID_FORMAT",
            message="version must be semver-style (e.g. '1.0.0')",
        ))

    # default_property_type / default_abstract_type are optional — validate if present
    dpt = doc.get("default_property_type")
    if dpt is not None and dpt not in VALID_PROPERTY_TYPES:
        result.errors.append(ValidationError(
            path="$.default_property_type", code="INVALID_VALUE",
            message=f"must be one of {sorted(VALID_PROPERTY_TYPES)}",
        ))

    # ---- fields[] ----
    fields_list = doc.get("fields")
    if not isinstance(fields_list, list) or not fields_list:
        result.errors.append(ValidationError(
            path="$.fields", code="MISSING_OR_EMPTY",
            message="'fields' must be a non-empty array",
        ))
        return result

    seen_field_ids: set[str] = set()
    for i, f in enumerate(fields_list):
        path_prefix = f"$.fields[{i}]"
        if not isinstance(f, dict):
            result.errors.append(ValidationError(
                path=path_prefix, code="NOT_OBJECT",
                message="field entry must be an object",
            ))
            continue

        # Two flavors: reference (use_playbook) or inline (full definition)
        if "use_playbook" in f:
            _validate_reference_field(
                f, path_prefix, available_playbook_ids,
                seen_field_ids, result,
            )
            result.references_count += 1
        elif "field_id" in f:
            _validate_inline_field(
                f, path_prefix, available_playbook_ids,
                seen_field_ids, result,
            )
            result.inline_count += 1
        else:
            result.errors.append(ValidationError(
                path=path_prefix, code="UNKNOWN_FIELD_SHAPE",
                message="field must contain either 'use_playbook' (reference) "
                        "or 'field_id' (inline custom field)",
            ))
        result.field_count += 1

    if not result.errors:
        result.ok = True
    return result


# ---------------------------------------------------------------------------
# Reference field: {"use_playbook": "tenant_name"}
# ---------------------------------------------------------------------------

def _validate_reference_field(
    f: dict,
    path: str,
    available: set[str],
    seen: set[str],
    result: ValidationResult,
) -> None:
    pb = f.get("use_playbook")
    if not isinstance(pb, str) or not pb:
        result.errors.append(ValidationError(
            path=f"{path}.use_playbook", code="MISSING",
            message="'use_playbook' must be a non-empty string",
        ))
        return
    if pb not in available:
        result.errors.append(ValidationError(
            path=f"{path}.use_playbook", code="UNKNOWN_PLAYBOOK",
            message=(
                f"playbook '{pb}' is not a registered built-in. Available "
                f"playbooks: {sorted(available)[:10]}"
                + (" ... (truncated)" if len(available) > 10 else "")
            ),
        ))
        return
    if pb in seen:
        result.errors.append(ValidationError(
            path=f"{path}.use_playbook", code="DUPLICATE_FIELD",
            message=f"field '{pb}' is referenced more than once in this schema",
        ))
    seen.add(pb)


# ---------------------------------------------------------------------------
# Inline field: full custom playbook
# ---------------------------------------------------------------------------

def _validate_inline_field(
    f: dict,
    path: str,
    available: set[str],
    seen: set[str],
    result: ValidationResult,
) -> None:
    fid = f.get("field_id")
    if not isinstance(fid, str) or not fid:
        result.errors.append(ValidationError(
            path=f"{path}.field_id", code="MISSING",
            message="'field_id' must be a non-empty string slug",
        ))
        return
    if not FIELD_ID_PATTERN.match(fid):
        result.errors.append(ValidationError(
            path=f"{path}.field_id", code="INVALID_FORMAT",
            message="field_id must match [a-z][a-z0-9_]{2,127}",
        ))
    if fid in seen:
        result.errors.append(ValidationError(
            path=f"{path}.field_id", code="DUPLICATE_FIELD",
            message=f"field_id '{fid}' is defined more than once in this schema",
        ))
    seen.add(fid)

    # Collision with built-in: only allowed if override=True
    if fid in available and not f.get("override", False):
        result.errors.append(ValidationError(
            path=f"{path}.field_id", code="COLLIDES_WITH_BUILTIN",
            message=(
                f"'{fid}' is the id of a built-in playbook. Either reference "
                f"it via {{\"use_playbook\": \"{fid}\"}}, or set 'override': "
                f"true on this entry to deliberately replace the built-in."
            ),
        ))

    # Required string fields
    for key in ("field_name", "category"):
        v = f.get(key)
        if not isinstance(v, str) or not v.strip():
            result.errors.append(ValidationError(
                path=f"{path}.{key}", code="MISSING",
                message=f"'{key}' is required (non-empty string)",
            ))

    # output_type
    ot = f.get("output_type", "Text")
    if ot not in VALID_OUTPUT_TYPES:
        result.errors.append(ValidationError(
            path=f"{path}.output_type", code="INVALID_VALUE",
            message=f"must be one of {sorted(VALID_OUTPUT_TYPES)}",
        ))

    # property_applicability (optional but if present must be valid)
    pa = f.get("property_applicability")
    if pa is not None:
        if not isinstance(pa, dict):
            result.errors.append(ValidationError(
                path=f"{path}.property_applicability", code="NOT_OBJECT",
                message="must be an object mapping property_type -> bool",
            ))
        else:
            for k, v in pa.items():
                if k not in VALID_PROPERTY_TYPES:
                    result.warnings.append(ValidationError(
                        path=f"{path}.property_applicability.{k}",
                        code="UNKNOWN_PROPERTY_TYPE",
                        message=f"unknown property type '{k}' (will be ignored)",
                    ))
                if not isinstance(v, bool):
                    result.errors.append(ValidationError(
                        path=f"{path}.property_applicability.{k}",
                        code="INVALID_VALUE",
                        message="value must be true/false",
                    ))

    # keywords
    kws = f.get("keywords", [])
    if not isinstance(kws, list):
        result.errors.append(ValidationError(
            path=f"{path}.keywords", code="NOT_ARRAY",
            message="'keywords' must be an array of strings",
        ))
    elif not kws:
        result.warnings.append(ValidationError(
            path=f"{path}.keywords", code="EMPTY",
            message="'keywords' is empty — extraction may rely entirely on the "
                    "vector retriever, which is less reliable for rare terms",
        ))

    # questions
    qs = f.get("questions")
    if not isinstance(qs, list) or not qs:
        result.errors.append(ValidationError(
            path=f"{path}.questions", code="MISSING_OR_EMPTY",
            message="'questions' must be a non-empty array",
        ))
        return

    seen_qids: set[str] = set()
    for j, q in enumerate(qs):
        qpath = f"{path}.questions[{j}]"
        if not isinstance(q, dict):
            result.errors.append(ValidationError(
                path=qpath, code="NOT_OBJECT",
                message="question must be an object",
            ))
            continue

        qid = q.get("id")
        if not isinstance(qid, str) or not qid:
            result.errors.append(ValidationError(
                path=f"{qpath}.id", code="MISSING",
                message="'id' is required (e.g. 'Q1')",
            ))
        elif qid in seen_qids:
            result.errors.append(ValidationError(
                path=f"{qpath}.id", code="DUPLICATE",
                message=f"question id '{qid}' is duplicated within this field",
            ))
        else:
            seen_qids.add(qid)

        if not isinstance(q.get("question_text"), str) or not q.get("question_text", "").strip():
            result.errors.append(ValidationError(
                path=f"{qpath}.question_text", code="MISSING",
                message="'question_text' is required (non-empty string)",
            ))

        ct = q.get("condition_type")
        if ct is not None and ct not in VALID_CONDITION_TYPES:
            result.warnings.append(ValidationError(
                path=f"{qpath}.condition_type", code="UNKNOWN_VALUE",
                message=f"non-standard condition_type '{ct}' "
                        f"(known: {sorted(VALID_CONDITION_TYPES)})",
            ))

        scope = q.get("search_scope", "all")
        if scope not in VALID_SEARCH_SCOPES:
            result.errors.append(ValidationError(
                path=f"{qpath}.search_scope", code="INVALID_VALUE",
                message=f"must be one of {sorted(VALID_SEARCH_SCOPES)}",
            ))

        qot = q.get("output_type", ot)
        if qot not in VALID_OUTPUT_TYPES:
            result.errors.append(ValidationError(
                path=f"{qpath}.output_type", code="INVALID_VALUE",
                message=f"must be one of {sorted(VALID_OUTPUT_TYPES)}",
            ))

        # Validate branches (yes_branch / no_branch)
        for branch_key in ("yes_branch", "no_branch"):
            b = q.get(branch_key)
            if b is None:
                continue
            if not isinstance(b, dict):
                result.errors.append(ValidationError(
                    path=f"{qpath}.{branch_key}", code="NOT_OBJECT",
                    message="branch must be an object",
                ))
                continue
            bt = b.get("type")
            if bt not in VALID_BRANCH_TYPES:
                result.errors.append(ValidationError(
                    path=f"{qpath}.{branch_key}.type", code="INVALID_VALUE",
                    message=f"branch type must be one of {sorted(VALID_BRANCH_TYPES)}",
                ))
            goto = b.get("goto")
            if goto is not None and goto not in seen_qids and goto not in {q.get("id") for q in qs}:
                # Forward-references are OK; the question may not be seen yet
                # We'll do a second pass below to check this properly
                pass
