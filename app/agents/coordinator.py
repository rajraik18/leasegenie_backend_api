"""Multi-agent coordinator.

Top-level orchestrator. For each tenant:
    1. Builds the DocumentContext (OCR + BM25 index across base + amendments).
    2. Auto-classifies each document (type + property type) so applicability
       gating works correctly even when the caller did not supply property_type.
    3. Dispatches to specialists in dependency order:
         Basic Information → Financial → Reimbursements → Critical → Other
       (Later specialists get shared_facts from earlier ones.)
    4. After all specialists finish, runs the ReconciliationAgent to merge
       per-document results into the final concluded values + red flags.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.agents.ollama_client import OllamaAgentClient
from app.agents.reconciliation_agent import ReconciliationAgent, ReconciliationReport
from app.agents.specialists import SPECIALISTS, FieldOutcome, SpecialistResult
from app.agents.tools import DocumentContext
from app.services.derived_fields import (
    DerivedField, cross_field_notes_for, derive_all,
)
from app.services.doc_classifier import classify_document, ClassificationResult
from app.services.ocr import Clause, extract_document_text, segment_clauses

logger = logging.getLogger(__name__)


# Order in which specialists run — matches BRD category order. Later ones may
# reference facts from earlier ones via shared_facts.
SPECIALIST_ORDER = [
    "Basic Information",
    "Financial Clauses",
    "Reimbursements",
    "Critical Clauses",
    "Other Lease Clauses",
]


@dataclass
class DocumentInput:
    document_id: str
    document_type: str                # "base_lease" | "amendment"
    document_order: int               # 0 = base, 1..7 = amendments
    storage_path: Path


@dataclass
class AgentFieldResult:
    document_id: str
    field_id: str
    field_name: str
    category: str
    value: str
    raw_value: str | None
    confidence: float
    source_doc: str | None
    page_number: int | None
    clause_number: str | None
    clause_text: str | None
    output_type: str
    condition_type_taken: str | None
    red_flags: list[str]
    needs_review: bool
    cross_field_notes: list[str]
    trace_summary: str                # compact human-readable step trace


class Coordinator:
    """Multi-agent coordinator — runs all five specialists for a tenant."""

    def __init__(
        self,
        client: OllamaAgentClient | None = None,
        enable_reconciliation: bool = True,
    ):
        if client is None:
            from app.agents.ollama_client import get_agent_client

            client = get_agent_client()
        self.client = client
        self.enable_reconciliation = enable_reconciliation

    def run(
        self,
        *,
        tenant_id: str,
        abstract_type: str,
        property_type: str,
        documents: list[DocumentInput],
        schema_doc: dict | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
        on_result: Callable[[AgentFieldResult], None] | None = None,
    ) -> tuple[list[AgentFieldResult], ReconciliationReport | None]:
        """Execute the multi-agent extraction.

        Returns (per-doc field results, reconciliation report).

        If `property_type` is empty or "Unknown", auto-infers it from the base
        lease document. If a document is detected as a sublease or amendment
        but input document_type says "base_lease", logs a warning.

        If `schema_doc` is provided, only the playbooks listed in the schema
        are run. Otherwise the full BRD playbook set is used.
        """
        if not documents:
            logger.info("no documents to extract")
            return [], None

        # 1. Build context (passes tenant_id so semantic_search tool can filter)
        ctx, label_to_id, classifications = self._build_context(documents, tenant_id)
        doc_labels = sorted(ctx.per_document_clauses.keys(),
                           key=lambda s: (0 if s == "base_lease" else 1, s))

        # 1b. Auto-infer property_type from base lease if not provided
        effective_property_type = property_type
        if not effective_property_type or effective_property_type.lower() in ("unknown", ""):
            base_classification = classifications.get("base_lease")
            if base_classification and base_classification.property_type != "Unknown":
                effective_property_type = base_classification.property_type
                logger.info(
                    "property_type auto-inferred as '%s' (confidence=%.2f) from base lease",
                    effective_property_type, base_classification.confidence,
                )
            else:
                logger.warning(
                    "property_type not provided and could not be inferred. "
                    "Retail-only playbooks will fire on all documents."
                )
                effective_property_type = "Office"  # conservative default

        # 1c. If a user schema is supplied, materialize the filtered playbook set
        playbooks_for_run: dict | None = None
        if schema_doc is not None:
            from app.agents.playbook_loader import load_playbooks_for_schema
            from app.agents.playbooks.loader import get_playbooks
            playbooks_for_run = load_playbooks_for_schema(
                schema_doc, builtin_playbooks=get_playbooks(),
            )
            logger.info(
                "using user schema '%s' v%s — %d playbook(s)",
                schema_doc.get("schema_id", "?"),
                schema_doc.get("version", "?"),
                len(playbooks_for_run),
            )

        shared_facts: dict[str, dict] = {}

        # 2. Pre-count total for progress
        total = self._count_total(
            abstract_type, effective_property_type, doc_labels,
            playbooks_override=playbooks_for_run,
        )
        done = 0
        all_outcomes: list[FieldOutcome] = []

        # 3. Run specialists in order
        for category in SPECIALIST_ORDER:
            SpecialistClass = SPECIALISTS.get(category)
            if SpecialistClass is None:
                continue
            specialist = SpecialistClass(self.client)
            specialist.set_playbooks(playbooks_for_run)
            logger.info("Running specialist: %s", category)

            # Custom run to emit per-field progress
            for pb in specialist.applicable_playbooks(abstract_type, effective_property_type):
                for doc_label in doc_labels:
                    try:
                        pr = specialist.executor.run(pb, ctx, doc_label, shared_facts=shared_facts)
                    except Exception as exc:
                        logger.exception("specialist %s failed on %s/%s",
                                         category, pb.field_id, doc_label)
                        pr = specialist._fallback_result(pb, str(exc))

                    outcome = FieldOutcome(
                        field_id=pb.field_id,
                        field_name=pb.field_name,
                        category=pb.category,
                        doc_label=doc_label,
                        playbook_result=pr,
                    )
                    specialist.post_process(outcome, shared_facts)
                    all_outcomes.append(outcome)

                    # Publish extracted facts to shared cache (base_lease only —
                    # amendments are handled by reconciliation)
                    if pr.value and pr.value.lower() != "none" and doc_label == "base_lease":
                        shared_facts.setdefault(pb.field_id, {})
                        shared_facts[pb.field_id][doc_label] = {
                            "value": pr.value,
                            "condition_type": pr.condition_type_taken,
                            "output_type": pr.output_type,
                        }

                    # Convert to API result type
                    doc_id = label_to_id.get(doc_label, doc_label)
                    result = AgentFieldResult(
                        document_id=doc_id,
                        field_id=pb.field_id,
                        field_name=pb.field_name,
                        category=pb.category,
                        value=pr.value,
                        raw_value=pr.raw_value,
                        confidence=pr.confidence,
                        source_doc=pr.source_doc,
                        page_number=pr.page_number,
                        clause_number=pr.clause_number,
                        clause_text=pr.clause_text,
                        output_type=pr.output_type,
                        condition_type_taken=pr.condition_type_taken,
                        red_flags=pr.red_flags,
                        needs_review=pr.needs_review,
                        cross_field_notes=outcome.cross_field_notes,
                        trace_summary=self._trace_summary(pr.trace),
                    )
                    if on_result is not None:
                        try:
                            on_result(result)
                        except Exception:
                            logger.exception("on_result callback failed")

                    done += 1
                    if on_progress is not None:
                        try:
                            on_progress(done, total, pb.field_id)
                        except Exception:
                            logger.exception("on_progress callback failed")

        # 4. Reconciliation
        report: ReconciliationReport | None = None
        if self.enable_reconciliation:
            report = ReconciliationAgent(self.client).run(
                outcomes=all_outcomes,
                documents=[(d.document_id, self._doc_label(d), d.document_type, d.document_order) for d in documents],
            )

        # 4b. Derived fields (start-date consolidation + property-address composition)
        derivations = self._run_derivations(all_outcomes)

        # 5. Emit final results
        final_results: list[AgentFieldResult] = []
        for outcome in all_outcomes:
            pr = outcome.playbook_result
            doc_id = label_to_id.get(outcome.doc_label, outcome.doc_label)

            # Attach derivation-aware cross-field notes to component fields
            per_field_map = self._per_field_map(all_outcomes, outcome.doc_label)
            extra_notes = cross_field_notes_for(outcome.field_id, per_field_map, derivations)
            combined_notes = list(outcome.cross_field_notes) + extra_notes

            final_results.append(AgentFieldResult(
                document_id=doc_id,
                field_id=outcome.field_id,
                field_name=outcome.field_name,
                category=outcome.category,
                value=pr.value,
                raw_value=pr.raw_value,
                confidence=pr.confidence,
                source_doc=pr.source_doc,
                page_number=pr.page_number,
                clause_number=pr.clause_number,
                clause_text=pr.clause_text,
                output_type=pr.output_type,
                condition_type_taken=pr.condition_type_taken,
                red_flags=pr.red_flags,
                needs_review=pr.needs_review,
                cross_field_notes=combined_notes,
                trace_summary=self._trace_summary(pr.trace),
            ))

        # Emit virtual derived fields (scoped to base_lease for display)
        base_doc_id = next(
            (d.document_id for d in documents if d.document_type == "base_lease"),
            documents[0].document_id,
        )
        for d in derivations:
            final_results.append(AgentFieldResult(
                document_id=base_doc_id,
                field_id=d.field_id,
                field_name=d.field_name,
                category=d.category,
                value=d.value,
                raw_value=None,
                confidence=d.confidence,
                source_doc=d.source_doc,
                page_number=d.page_number,
                clause_number=None,
                clause_text=d.clause_text,
                output_type="Derived",
                condition_type_taken=None,
                red_flags=[],
                needs_review=False,
                cross_field_notes=[d.note],
                trace_summary=f"derived from {d.source_field_id}",
            ))

        return final_results, report

    # ------------------------------------------------------------------

    def _run_derivations(self, all_outcomes: list[FieldOutcome]) -> list[DerivedField]:
        """Run derived-field consolidation on the base_lease's outcomes.

        Only base-lease values are considered canonical here; amendment
        overrides are already handled by the ReconciliationAgent.
        """
        base_map = self._per_field_map(all_outcomes, doc_label="base_lease")
        return derive_all(base_map)

    @staticmethod
    def _per_field_map(all_outcomes: list[FieldOutcome], doc_label: str) -> dict[str, dict]:
        """Build {field_id: {value, confidence, source_doc, page_number, clause_text}}
        filtered to one document's outcomes."""
        out: dict[str, dict] = {}
        for oc in all_outcomes:
            if oc.doc_label != doc_label:
                continue
            pr = oc.playbook_result
            out[oc.field_id] = {
                "value": pr.value,
                "confidence": pr.confidence,
                "source_doc": pr.source_doc,
                "page_number": pr.page_number,
                "clause_text": pr.clause_text,
            }
        return out

    def _build_context(
        self,
        documents: list[DocumentInput],
        tenant_id: str | None = None,
    ) -> tuple[DocumentContext, dict[str, str], dict[str, ClassificationResult]]:
        per_doc: dict[str, list[Clause]] = {}
        label_to_id: dict[str, str] = {}
        classifications: dict[str, ClassificationResult] = {}
        for doc in documents:
            label = self._doc_label(doc)
            label_to_id[label] = doc.document_id
            try:
                dt = extract_document_text(doc.storage_path)
                per_doc[label] = segment_clauses(dt)
                # Classify on first ~15 pages of text
                sample_text = "\n".join(
                    p.text for p in dt.pages[:15] if p.text
                )
                classification = classify_document(
                    sample_text,
                    filename_hint=doc.storage_path.name,
                )
                classifications[label] = classification

                # Sanity check: if input says base_lease but we detect amendment/sublease, warn
                if doc.document_type == "base_lease" and classification.document_type in ("amendment", "sublease"):
                    logger.warning(
                        "Document %s labeled as base_lease but classifier detected '%s' "
                        "(conf=%.2f). Consider re-labeling.",
                        doc.document_id, classification.document_type, classification.confidence,
                    )
                logger.info(
                    "Classified %s: doc_type=%s, property_type=%s, conf=%.2f",
                    label, classification.document_type,
                    classification.property_type, classification.confidence,
                )
            except Exception:
                logger.exception("OCR/classification failed for %s", doc.storage_path)
                per_doc[label] = []
        ctx = DocumentContext(per_document_clauses=per_doc, tenant_id=tenant_id)
        ctx.build_index()
        return ctx, label_to_id, classifications

    def _count_total(
        self, abstract_type: str, property_type: str, doc_labels: list[str],
        *, playbooks_override: dict | None = None,
    ) -> int:
        total = 0
        for category in SPECIALIST_ORDER:
            SpecialistClass = SPECIALISTS.get(category)
            if SpecialistClass is None:
                continue
            spec = SpecialistClass(self.client)
            spec.set_playbooks(playbooks_override)
            pbs = spec.applicable_playbooks(abstract_type, property_type)
            total += len(pbs) * len(doc_labels)
        return total

    @staticmethod
    def _doc_label(d: DocumentInput) -> str:
        if d.document_type == "base_lease":
            return "base_lease"
        return f"amendment_{d.document_order}"

    @staticmethod
    def _trace_summary(trace: list) -> str:
        if not trace:
            return "(no trace)"
        parts = []
        for step in trace:
            parts.append(
                f"{step.qid}({step.llm_answer})→{step.branch_taken}→{step.next_action}"
            )
        return " | ".join(parts)
