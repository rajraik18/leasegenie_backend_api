"""Reimbursements specialist.

Handles: CAM, RE Taxes, Landlord Insurance, Tenant Insurance, Utilities,
Admin Fee, Mgmt Fee, Advertisement, Marketing, Other Income, Base Year,
Base Year Amount, Gross-Up, CAM Inclusion/Exclusion, Caps on CAM, Pro Rata %.

Post-processing:
    * If CAM is "No Recovery", propagate "No Recovery" to dependent fields
      (CAM Inclusion, CAM Exclusion, Caps on CAM) unless they have their own
      explicit extraction.
    * Gross/Modified Gross/Net categorization — stored as a shared fact.
"""
from __future__ import annotations

from typing import Any

from app.agents.specialists.base import FieldOutcome, SpecialistAgent


class ReimbursementAgent(SpecialistAgent):
    category = "Reimbursements"

    def post_process(self, outcome: FieldOutcome, shared_facts: dict[str, Any]) -> None:
        pr = outcome.playbook_result

        # When CAM is "No Recovery" → propagate to CAM-related fields
        if outcome.field_id == "cam" and pr.value.strip().lower() == "no recovery":
            shared_facts.setdefault("_cam_structure", {})[outcome.doc_label] = "No Recovery"

        # If CAM was determined "No Recovery" on this doc, downstream CAM fields
        # should inherit that unless the playbook found its own explicit answer.
        if outcome.field_id in ("cam_inclusion", "cam_exclusion", "caps_on_cam",
                                 "gross_up", "base_year", "base_year_amount"):
            cam_struct = shared_facts.get("_cam_structure", {}).get(outcome.doc_label)
            if cam_struct == "No Recovery" and pr.value == "None":
                pr.value = "No Recovery"
                outcome.cross_field_notes.append(
                    "Inherited 'No Recovery' from CAM structure"
                )
