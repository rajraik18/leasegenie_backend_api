"""Financial Clauses specialist.

Handles: Annual Base Rent, Future Rent Steps, Percentage Rent, Breakpoint,
Security Deposit.

Post-processing:
    * Ensures Currency fields are numeric (monthly × 12 is applied in executor)
    * For Percentage Rent: only emitted for retail property types (the playbook
      gate handles this, but we double-enforce)
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.specialists.base import FieldOutcome, SpecialistAgent

logger = logging.getLogger(__name__)


RETAIL_ONLY = {"percentage_rent", "breakpoint"}


class FinancialAgent(SpecialistAgent):
    category = "Financial Clauses"

    def post_process(self, outcome: FieldOutcome, shared_facts: dict[str, Any]) -> None:
        pr = outcome.playbook_result

        # Confidence floor for Currency outputs that don't have a raw snippet
        if pr.output_type.lower() in ("currency", "number") and pr.value != "None":
            if not pr.raw_value and pr.confidence > 0.6:
                pr.confidence = 0.6
                outcome.cross_field_notes.append("Currency value lacks verbatim snippet — confidence reduced")
