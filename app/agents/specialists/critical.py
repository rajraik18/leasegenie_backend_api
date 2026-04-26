"""Critical Lease Clauses specialist.

Handles: Tenant Termination, Landlord Termination, Recapture, Go-Dark,
Sales Kick-Out, Co-Tenancy, Renewal, Holdover (moved to Other), ROFO, ROFR,
Right of Expansion, Purchase Option, Contraction, Exclusive Use, Permitted
Use, Continuous Operation, Relocation.

Post-processing:
    * Most fields are Extract-Clause-or-None — no numeric normalization needed
    * Co-Tenancy only applies to retail (enforced by playbook)
"""
from __future__ import annotations

from typing import Any

from app.agents.specialists.base import FieldOutcome, SpecialistAgent


class CriticalAgent(SpecialistAgent):
    category = "Critical Clauses"

    def post_process(self, outcome: FieldOutcome, shared_facts: dict[str, Any]) -> None:
        pr = outcome.playbook_result

        # Critical clauses extracted verbatim — cap clause_text to 4000 chars for sanity
        if pr.clause_text and len(pr.clause_text) > 4000:
            pr.clause_text = pr.clause_text[:4000] + " …[truncated]"
