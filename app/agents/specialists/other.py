"""Other Lease Clauses specialist.

Handles: Allowance, Alteration, Assignment and Subletting, Casualty,
Condemnation, Hazardous Materials, Holdover, Landlord Restriction, Monetary
Default, Non-Monetary Default, Parking, Repair and Maintenance, Reporting of
Financial Information, Reporting of Gross Sales, Sublease Provision,
Subordination.

Post-processing:
    * Allowance depends on LCD (per the docx): if LCD is within 1 year of today
      extract full disbursement language; otherwise amount-only. The playbook
      executor already captured this via two questions, but we append a note
      for clarity.
    * Reporting of Gross Sales is retail-only (playbook-gated).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.agents.specialists.base import FieldOutcome, SpecialistAgent


class OtherClausesAgent(SpecialistAgent):
    category = "Other Lease Clauses"

    def post_process(self, outcome: FieldOutcome, shared_facts: dict[str, Any]) -> None:
        pr = outcome.playbook_result

        if outcome.field_id == "allowance":
            lcd_fact = shared_facts.get("original_lease_commencement_date", {}).get("base_lease")
            if lcd_fact and pr.value != "None":
                lcd_str = lcd_fact.get("value", "")
                within_year = _within_one_year(lcd_str)
                outcome.cross_field_notes.append(
                    f"LCD={lcd_str} — {'recent (<1yr)' if within_year else 'older (>1yr)'} → "
                    f"{'full disbursement terms expected' if within_year else 'amount-only extraction'}"
                )

        # Cap clause_text for sanity
        if pr.clause_text and len(pr.clause_text) > 4000:
            pr.clause_text = pr.clause_text[:4000] + " …[truncated]"


def _within_one_year(date_str: str) -> bool:
    try:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
            try:
                d = datetime.strptime(date_str.strip(), fmt)
                return (datetime.utcnow() - d) <= timedelta(days=365)
            except ValueError:
                continue
    except Exception:
        pass
    return False
