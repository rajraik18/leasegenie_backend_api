"""Base class for specialist agents."""
from __future__ import annotations

import logging
from abc import ABC
from dataclasses import dataclass, field
from typing import Any

from app.agents.ollama_client import OllamaAgentClient
from app.agents.playbook_executor import PlaybookExecutor, PlaybookResult
from app.agents.playbooks import Playbook, get_playbooks
from app.agents.tools import DocumentContext

logger = logging.getLogger(__name__)


@dataclass
class FieldOutcome:
    """Per-field outcome the coordinator persists."""
    field_id: str
    field_name: str
    category: str
    doc_label: str                         # which document this result is for
    playbook_result: PlaybookResult
    cross_field_notes: list[str] = field(default_factory=list)


@dataclass
class SpecialistResult:
    category: str
    outcomes: list[FieldOutcome] = field(default_factory=list)


class SpecialistAgent(ABC):
    """One specialist per BRD category."""
    category: str = ""

    def __init__(self, client: OllamaAgentClient):
        self.client = client
        self.executor = PlaybookExecutor(client)
        # When set (by Coordinator), this overrides get_playbooks() so the
        # specialist only runs the schema-selected playbooks for this category.
        self._playbooks_override: dict[str, Playbook] | None = None

    def set_playbooks(self, playbooks: dict[str, Playbook] | None) -> None:
        """Override the playbook source. None reverts to built-in get_playbooks()."""
        self._playbooks_override = playbooks

    def applicable_playbooks(
        self,
        abstract_type: str,
        property_type: str,
    ) -> list[Playbook]:
        """Return the playbooks for this specialist's category, filtered by
        applicability. If a playbook has no applicability matrix, it's
        considered always-applicable for its category."""
        if self._playbooks_override is not None:
            all_pbs = self._playbooks_override.values()
        else:
            all_pbs = get_playbooks().values()
        out: list[Playbook] = []
        for pb in all_pbs:
            if pb.category != self.category:
                continue
            # Property-type gate
            if pb.property_applicability and property_type:
                if not pb.property_applicability.get(property_type, False):
                    # Mixed-Use: if no explicit Mixed-Use key, allow whenever any
                    # of Retail/Industrial/Office is true
                    if property_type == "Mixed-Use":
                        if not any(pb.property_applicability.get(k, False)
                                   for k in ("Retail", "Industrial", "Office")):
                            continue
                    else:
                        continue
            out.append(pb)
        out.sort(key=lambda p: p.field_name)
        return out

    def run(
        self,
        *,
        abstract_type: str,
        property_type: str,
        ctx: DocumentContext,
        doc_labels: list[str],
        shared_facts: dict[str, Any],
    ) -> SpecialistResult:
        """Execute every applicable playbook against every document.

        `shared_facts` accumulates across fields so later playbooks can
        reference facts from earlier ones (e.g. Allowance needs LCD).
        """
        result = SpecialistResult(category=self.category)

        for pb in self.applicable_playbooks(abstract_type, property_type):
            for doc_label in doc_labels:
                try:
                    pr = self.executor.run(pb, ctx, doc_label, shared_facts=shared_facts)
                except Exception as exc:
                    logger.exception("specialist %s failed on %s/%s",
                                     self.category, pb.field_id, doc_label)
                    pr = self._fallback_result(pb, str(exc))

                outcome = FieldOutcome(
                    field_id=pb.field_id,
                    field_name=pb.field_name,
                    category=pb.category,
                    doc_label=doc_label,
                    playbook_result=pr,
                )
                self.post_process(outcome, shared_facts)
                result.outcomes.append(outcome)

                # Publish extracted facts for downstream specialists
                if pr.value and pr.value.lower() != "none" and doc_label == "base_lease":
                    shared_facts.setdefault(pb.field_id, {})
                    shared_facts[pb.field_id][doc_label] = {
                        "value": pr.value,
                        "condition_type": pr.condition_type_taken,
                        "output_type": pr.output_type,
                    }

        return result

    # Subclasses override to apply category-specific rules
    def post_process(self, outcome: FieldOutcome, shared_facts: dict[str, Any]) -> None:
        pass

    @staticmethod
    def _fallback_result(pb: Playbook, err: str) -> PlaybookResult:
        return PlaybookResult(
            field_id=pb.field_id,
            value="None",
            raw_value=None,
            confidence=0.0,
            source_doc=None,
            page_number=None,
            clause_number=None,
            clause_text=f"[agent error: {err}]",
            output_type=pb.output_type,
        )
