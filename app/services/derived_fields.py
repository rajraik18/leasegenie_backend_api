"""Derived-field consolidator.

The project's 71 BRD playbooks include three overlapping "start date" fields
and three overlapping "address" fields. After the specialists finish, this
module derives a single canonical value for each concept using a
priority-based resolver, and attaches cross-field notes to each outcome so
the UI can show the derivation.

Start-date fields (pick best from these, in priority order):
    1. original_lease_commencement_date      — the historical ORIGINAL start
    2. term_commencement_date                — term clock begins
    3. rent_commencement_date                — rent clock begins (may differ)
    4. most_recent_lease_start               — effective after amendments

Canonical derivation: the lease's "true" start date is
    original_lease_commencement_date  (if present)
    else term_commencement_date
    else rent_commencement_date

We expose the canonical under a virtual `canonical_commencement_date` field
and annotate each of the 3 components with cross-field notes naming the
winner and the others.

Address fields:
    street_address + city + state  →  property_address (combined)

We leave the three components in place (many downstream consumers need them
split) but add the combined virtual `property_address`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Start-date consolidation
# ---------------------------------------------------------------------------

START_DATE_FIELDS = [
    "original_lease_commencement_date",
    "term_commencement_date",
    "rent_commencement_date",
    "most_recent_lease_start",
]

# Priority order (first = best)
START_DATE_PRIORITY = [
    "original_lease_commencement_date",
    "term_commencement_date",
    "rent_commencement_date",
    "most_recent_lease_start",
]


# ---------------------------------------------------------------------------
# Address consolidation
# ---------------------------------------------------------------------------

ADDRESS_FIELDS = ["street_address", "city", "state"]


@dataclass
class DerivedField:
    field_id: str
    field_name: str
    category: str
    value: str
    confidence: float
    source_field_id: str | None        # which input field this was derived from
    source_doc: str | None
    page_number: int | None
    clause_text: str | None
    note: str                           # explanation for audit


def _is_real_value(v: Any) -> bool:
    if v is None:
        return False
    s = str(v).strip()
    return bool(s) and s.lower() not in {"none", "n/a", "unknown", "not found"}


def derive_canonical_commencement_date(
    per_field_results: dict[str, dict],
) -> DerivedField | None:
    """Given the extracted values for the 4 start-date fields, pick canonical.

    `per_field_results` is keyed by field_id; each value is a dict with
    at least {value, confidence, source_doc, page_number, clause_text}.
    """
    for candidate_id in START_DATE_PRIORITY:
        r = per_field_results.get(candidate_id)
        if not r:
            continue
        if not _is_real_value(r.get("value")):
            continue

        winning_value = r["value"]

        # Build note explaining derivation
        note_parts = [
            f"Canonical commencement derived from {candidate_id!r} = {winning_value!r}"
        ]
        overrides: list[str] = []
        for other_id in START_DATE_FIELDS:
            if other_id == candidate_id:
                continue
            other = per_field_results.get(other_id)
            if other and _is_real_value(other.get("value")):
                other_val = other.get("value")
                if _dates_differ(other_val, winning_value):
                    overrides.append(f"{other_id}={other_val!r}")
        if overrides:
            note_parts.append(
                "Other date fields present but overridden: " + ", ".join(overrides)
            )

        return DerivedField(
            field_id="canonical_commencement_date",
            field_name="Canonical Commencement Date",
            category="Basic Information",
            value=winning_value,
            confidence=r.get("confidence", 0.7),
            source_field_id=candidate_id,
            source_doc=r.get("source_doc"),
            page_number=r.get("page_number"),
            clause_text=r.get("clause_text"),
            note="; ".join(note_parts),
        )

    return None


def _dates_differ(a: str, b: str) -> bool:
    """Return True iff two date strings refer to different calendar dates."""
    if a == b:
        return False
    # Normalize both to yyyy-mm-dd if possible, then compare
    na = _normalize_date(a)
    nb = _normalize_date(b)
    if na and nb:
        return na != nb
    return str(a).strip().lower() != str(b).strip().lower()


_MONTH_NAME_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

_DATE_NAME_RE = re.compile(
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(?P<d>\d{1,2}),?\s+(?P<y>\d{4})",
    re.IGNORECASE,
)
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


def _normalize_date(s: str) -> str | None:
    if not s:
        return None
    m = _ISO_DATE_RE.search(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = _DATE_NAME_RE.search(s)
    if m:
        mon = _MONTH_NAME_MAP.get(m.group("mon").lower()[:3], "01")
        return f"{m.group('y')}-{mon}-{int(m.group('d')):02d}"
    m = _DATE_NUM_RE.search(s)
    if m:
        mo, d, y = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = ("20" if int(y) < 50 else "19") + y
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


# ---------------------------------------------------------------------------
# Address consolidation
# ---------------------------------------------------------------------------

def derive_property_address(per_field_results: dict[str, dict]) -> DerivedField | None:
    street = per_field_results.get("street_address", {})
    city = per_field_results.get("city", {})
    state = per_field_results.get("state", {})

    parts: list[str] = []
    source_ids: list[str] = []
    best_conf = 0.0
    best_source_doc = None
    best_page = None
    best_clause = None

    for fid, r in (("street_address", street), ("city", city), ("state", state)):
        v = r.get("value")
        if _is_real_value(v):
            parts.append(str(v).strip().rstrip(","))
            source_ids.append(fid)
            if (r.get("confidence") or 0) > best_conf:
                best_conf = r.get("confidence", 0)
                best_source_doc = r.get("source_doc")
                best_page = r.get("page_number")
                best_clause = r.get("clause_text")

    if not parts:
        return None

    # Format: "<street>, <city>, <state>"
    if len(parts) == 3:
        combined = f"{parts[0]}, {parts[1]}, {parts[2]}"
    elif len(parts) == 2:
        combined = f"{parts[0]}, {parts[1]}"
    else:
        combined = parts[0]

    return DerivedField(
        field_id="property_address",
        field_name="Property Address (derived)",
        category="Basic Information",
        value=combined,
        confidence=best_conf,
        source_field_id=",".join(source_ids),
        source_doc=best_source_doc,
        page_number=best_page,
        clause_text=best_clause,
        note=f"Composed from: {', '.join(source_ids)}",
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def derive_all(
    per_field_results: dict[str, dict],
) -> list[DerivedField]:
    """Return all derived fields that can be computed from the inputs."""
    out: list[DerivedField] = []
    canon = derive_canonical_commencement_date(per_field_results)
    if canon is not None:
        out.append(canon)
    addr = derive_property_address(per_field_results)
    if addr is not None:
        out.append(addr)
    return out


def cross_field_notes_for(
    field_id: str,
    per_field_results: dict[str, dict],
    derivations: list[DerivedField],
) -> list[str]:
    """Given a source field, return human-readable notes describing how its
    value interacts with canonical/derived fields. Attach these to each
    component field's cross_field_notes so the UI can show provenance."""
    notes: list[str] = []
    for d in derivations:
        if field_id == d.source_field_id or (d.source_field_id and field_id in d.source_field_id.split(",")):
            notes.append(f"→ feeds derived field '{d.field_id}' = {d.value!r}")
        # For the non-winning start-date fields, mention they were overridden
        if d.field_id == "canonical_commencement_date" and field_id in START_DATE_FIELDS:
            if field_id != d.source_field_id:
                this = per_field_results.get(field_id, {})
                if _is_real_value(this.get("value")):
                    notes.append(
                        f"← overridden by canonical start date "
                        f"'{d.source_field_id}'={d.value!r}"
                    )
    return notes
