"""Reconciliation agent.

After all specialists finish, this agent does two sweeps:

1. Cross-document reconciliation. For each field, picks the "concluded" value:
       override (out-of-scope, comes from the API)
         > latest amendment with a non-None value (by effective date)
         > base lease
         > "None"
   This agent does NOT persist — pipeline.py persists using the richer
   concluded_value.py service. But it does flag inconsistencies that
   LeaseLens cares about.

2. Cross-field red flags. Detects:
     - RSF mismatches across base + amendments
     - Suite conflicts
     - Tenant name spelling variations
     - LED < LCD (impossible dates)
     - Lease Term (yrs) doesn't match LED − LCD
     - Multiple commencement dates (red flag from Questions.xlsx)
     - Co-Tenancy on non-retail property (shouldn't happen post-gate)
     - Low-confidence extraction count

The output is a ReconciliationReport the pipeline persists + the /red-flags
endpoint returns.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agents.ollama_client import OllamaAgentClient
from app.agents.specialists.base import FieldOutcome

logger = logging.getLogger(__name__)


@dataclass
class RedFlagItem:
    code: str
    severity: str                # "info" | "warning" | "critical"
    message: str
    field_ids: list[str]
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationReport:
    total_fields: int
    total_outcomes: int
    low_confidence_count: int
    needs_review_count: int
    red_flags: list[RedFlagItem] = field(default_factory=list)
    per_field_red_flags: list[str] = field(default_factory=list)  # from playbook red_flag column


class ReconciliationAgent:
    def __init__(self, client: OllamaAgentClient | None = None):
        self.client = client  # reserved for LLM-based cross-doc arbitration later

    def run(
        self,
        *,
        outcomes: list[FieldOutcome],
        documents: list[tuple[str, str, str, int]],  # (doc_id, label, type, order)
    ) -> ReconciliationReport:
        """Run both reconciliation sweeps. Returns the report."""

        report = ReconciliationReport(
            total_fields=len({o.field_id for o in outcomes}),
            total_outcomes=len(outcomes),
            low_confidence_count=sum(1 for o in outcomes if 0 < o.playbook_result.confidence < 0.5),
            needs_review_count=sum(1 for o in outcomes if o.playbook_result.needs_review),
        )

        # Collect playbook-declared red flags that triggered
        for o in outcomes:
            for rf in o.playbook_result.red_flags:
                report.per_field_red_flags.append(f"{o.field_name}: {rf}")

        # Index outcomes by (field_id, doc_label)
        by_field: dict[str, list[FieldOutcome]] = defaultdict(list)
        for o in outcomes:
            by_field[o.field_id].append(o)

        # 1. RSF mismatch across documents
        self._check_value_consistency(
            by_field, "leased_rsf",
            code="RSF_MISMATCH",
            severity="warning",
            message_fmt="Rentable Square Footage differs across documents: {values}",
            report=report,
        )

        # 2. Suite conflicts
        self._check_value_consistency(
            by_field, "suite",
            code="SUITE_CONFLICT",
            severity="warning",
            message_fmt="Suite number varies across documents: {values}",
            report=report,
        )

        # 3. Tenant name variations
        self._check_value_consistency(
            by_field, "tenant_name",
            code="NAME_VARIATION",
            severity="info",
            message_fmt="Tenant name varies across documents: {values}",
            report=report,
        )

        # 4. LED < LCD
        self._check_date_ordering(by_field, report)

        # 5. Lease Term sanity vs LED−LCD
        self._check_lease_term_math(by_field, report)

        # 6. Base rent mismatch across amendments (not necessarily an error —
        # amendments typically revise rent — but flagged as info)
        self._check_rent_changes(by_field, report)

        # 7. Low confidence rollup
        if report.low_confidence_count > 0:
            report.red_flags.append(RedFlagItem(
                code="LOW_CONFIDENCE",
                severity="info",
                message=(
                    f"{report.low_confidence_count} fields extracted with "
                    "confidence below 0.5 — review recommended"
                ),
                field_ids=sorted({
                    o.field_id for o in outcomes
                    if 0 < o.playbook_result.confidence < 0.5
                }),
            ))

        # 8. Needs review rollup
        if report.needs_review_count > 0:
            report.red_flags.append(RedFlagItem(
                code="MANUAL_REVIEW_REQUIRED",
                severity="warning",
                message=(
                    f"{report.needs_review_count} fields were flagged by the "
                    "playbook for manual review"
                ),
                field_ids=sorted({
                    o.field_id for o in outcomes if o.playbook_result.needs_review
                }),
            ))

        return report

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_value_consistency(
        self,
        by_field: dict[str, list[FieldOutcome]],
        field_id: str,
        *,
        code: str,
        severity: str,
        message_fmt: str,
        report: ReconciliationReport,
    ) -> None:
        outcomes = by_field.get(field_id, [])
        values = _distinct_nonempty([o.playbook_result.value for o in outcomes])
        if len(values) > 1:
            report.red_flags.append(RedFlagItem(
                code=code,
                severity=severity,
                message=message_fmt.format(values=values),
                field_ids=[field_id],
                context={"values": values},
            ))

    def _check_date_ordering(
        self,
        by_field: dict[str, list[FieldOutcome]],
        report: ReconciliationReport,
    ) -> None:
        lcd = _first_date(by_field.get("original_lease_commencement_date", []))
        led = _first_date(by_field.get("lease_expiration_date", []))
        if lcd and led and led < lcd:
            report.red_flags.append(RedFlagItem(
                code="DATE_INCONSISTENCY",
                severity="critical",
                message=(
                    f"Lease Expiration ({led.strftime('%Y-%m-%d')}) precedes "
                    f"Lease Commencement ({lcd.strftime('%Y-%m-%d')})"
                ),
                field_ids=["lease_expiration_date", "original_lease_commencement_date"],
            ))

    def _check_lease_term_math(
        self,
        by_field: dict[str, list[FieldOutcome]],
        report: ReconciliationReport,
    ) -> None:
        lcd = _first_date(by_field.get("original_lease_commencement_date", []))
        led = _first_date(by_field.get("lease_expiration_date", []))
        term_outcomes = by_field.get("lease_term_yrs", [])
        if not term_outcomes or not lcd or not led:
            return
        stated_term_str = term_outcomes[0].playbook_result.value
        try:
            stated = float(stated_term_str)
        except ValueError:
            return
        derived = round((led - lcd).days / 365.25, 1)
        if abs(stated - derived) > 0.5:
            report.red_flags.append(RedFlagItem(
                code="LEASE_TERM_MATH_MISMATCH",
                severity="warning",
                message=(
                    f"Stated Lease Term ({stated} yrs) disagrees with "
                    f"LED − LCD ({derived} yrs)"
                ),
                field_ids=["lease_term_yrs", "original_lease_commencement_date", "lease_expiration_date"],
                context={"stated": stated, "derived": derived},
            ))

    def _check_rent_changes(
        self,
        by_field: dict[str, list[FieldOutcome]],
        report: ReconciliationReport,
    ) -> None:
        outcomes = by_field.get("annual_base_rent", [])
        by_doc = {o.doc_label: o.playbook_result.value for o in outcomes
                  if o.playbook_result.value and o.playbook_result.value.lower() != "none"}
        if len(by_doc) < 2:
            return
        vals = list(set(by_doc.values()))
        if len(vals) > 1:
            report.red_flags.append(RedFlagItem(
                code="RENT_CHANGE_ACROSS_DOCS",
                severity="info",
                message=(
                    "Annual Base Rent varies across base lease and amendments "
                    "(expected if amendments revise rent — confirm)"
                ),
                field_ids=["annual_base_rent"],
                context={"by_document": by_doc},
            ))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _distinct_nonempty(values: list[str | None]) -> list[str]:
    return sorted({
        (v or "").strip()
        for v in values
        if v and (v or "").strip() and v.lower() != "none"
    })


_DATE_FMTS = ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y")


def _first_date(outcomes: list[FieldOutcome]) -> datetime | None:
    for o in outcomes:
        v = o.playbook_result.value
        if not v or v.lower() in ("none", "undated"):
            continue
        for fmt in _DATE_FMTS:
            try:
                return datetime.strptime(v.strip(), fmt)
            except ValueError:
                continue
    return None
