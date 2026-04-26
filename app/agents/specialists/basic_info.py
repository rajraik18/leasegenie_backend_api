"""Basic Information specialist.

Handles: Tenant Name, Landlord Name, Building, Suite, Property Name, Street
Address, City, State, Lease Date, Lease Guarantor, LCD, LED, TCD, RCD, Lease
Term, Leased RSF, Most Recent Lease Start.

Post-processing:
    * Tenant Name must never be blank — if playbook returned None, mark for review
    * Lease Term is derived from LCD + LED when both are present
    * Lease Date: if absent, record literal "undated"
    * Dates: normalize to MM/DD/YYYY
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.agents.specialists.base import FieldOutcome, SpecialistAgent


MUST_HAVE_FIELDS = {"tenant_name", "landlord_name"}


class BasicInfoAgent(SpecialistAgent):
    category = "Basic Information"

    def post_process(self, outcome: FieldOutcome, shared_facts: dict[str, Any]) -> None:
        pr = outcome.playbook_result

        # Never-blank rule for must-haves
        if outcome.field_id in MUST_HAVE_FIELDS and pr.value.lower() == "none":
            pr.needs_review = True
            pr.red_flags.append(f"{outcome.field_name} is blank — every lease must have one")

        # Lease Date → "undated" literal if still None after playbook walk
        if outcome.field_id == "lease_date" and pr.value.lower() == "none":
            pr.value = "undated"

        # Lease Term (yrs.) = LED − LCD if both available (base lease only)
        if outcome.field_id == "lease_term_yrs" and outcome.doc_label == "base_lease":
            lcd = _get_fact(shared_facts, "original_lease_commencement_date", "base_lease")
            led = _get_fact(shared_facts, "lease_expiration_date", "base_lease")
            if lcd and led and pr.value.lower() == "none":
                derived = _years_between(lcd, led)
                if derived is not None:
                    pr.value = str(derived)
                    pr.raw_value = f"derived from LCD={lcd} / LED={led}"
                    pr.confidence = 0.85
                    outcome.cross_field_notes.append(
                        f"Lease Term derived from LCD={lcd} and LED={led}"
                    )

        # Normalize dates
        if "Date" in outcome.field_name and pr.value.lower() not in ("none", "undated"):
            norm = _normalize_date(pr.value)
            if norm:
                pr.value = norm


def _get_fact(shared: dict, field_id: str, doc_label: str) -> str | None:
    entry = shared.get(field_id, {}).get(doc_label)
    return entry["value"] if entry else None


_DATE_PATTERNS = [
    # ISO
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), "%Y-%m-%d"),
    # MM/DD/YYYY
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})"), "%m/%d/%Y"),
    # Month DD, YYYY
    (re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})"), "%B %d, %Y"),
]


def _normalize_date(s: str) -> str | None:
    s = s.strip().replace("  ", " ")
    for pat, fmt in _DATE_PATTERNS:
        if pat.match(s):
            try:
                # Handle abbreviated months too
                for try_fmt in (fmt, fmt.replace("%B", "%b")):
                    try:
                        d = datetime.strptime(s[: len(s)].split("\n")[0].strip().rstrip(".,"), try_fmt)
                        return d.strftime("%m/%d/%Y")
                    except ValueError:
                        continue
            except Exception:
                continue
    return None


def _years_between(lcd: str, led: str) -> float | None:
    try:
        d1 = _parse_any_date(lcd)
        d2 = _parse_any_date(led)
        if not d1 or not d2:
            return None
        years = (d2 - d1).days / 365.25
        return round(years, 1)
    except Exception:
        return None


def _parse_any_date(s: str) -> datetime | None:
    for pat, fmt in _DATE_PATTERNS:
        if pat.match(s):
            for try_fmt in (fmt, fmt.replace("%B", "%b")):
                try:
                    return datetime.strptime(s.strip(), try_fmt)
                except ValueError:
                    continue
    return None
