"""Demonstration: run the eval harness with simulated extraction results
to validate the scoring logic end-to-end without requiring live Ollama.

Simulates the v2.0 pipeline producing a mix of:
    - Correct exact matches on easy fields (difficulty 1-2)
    - Correct partial matches on hard fields (difficulty 3-4)
    - A few realistic wrong answers that exercise each failure mode
    - Properly-gated retail-only fields returning None

Output: Markdown report that shows what production evaluation will look like.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.eval.ground_truth_sample6 import SAMPLE_6_GROUND_TRUTH
from tests.eval.harness import (
    build_aggregate_report,
    evaluate_document,
    report_to_markdown,
)


def _mk_result(field_id, value, confidence, page=None, clause_text=None):
    """Mock AgentFieldResult."""
    return SimpleNamespace(
        field_id=field_id,
        value=value,
        confidence=confidence,
        page_number=page,
        clause_text=clause_text,
    )


# Simulated extraction — mimics v2.0 performance on Sample 6.
# Most fields correct; a handful of realistic failures.
SIMULATED_RESULTS = [
    # === Basic Information - mostly correct ===
    _mk_result("tenant_name",
               "GARNER T.V. & APPLIANCES INC. (dba GARNER APPLIANCE & MATTRESS)",
               0.97, 1, "GARNER T.V. & APPLIANCES INC."),
    _mk_result("landlord_name", "HMBP - BCP LLC", 0.97, 1, "HMBP - BCP LLC"),
    _mk_result("lease_date", "August 2, 2024", 0.95, 1, "8/2/2024"),
    _mk_result("original_lease_commencement_date", "August 1, 2024", 0.93, 2,
               "shall be August 1, 2024"),
    _mk_result("lease_expiration_date", "September 30, 2029", 0.85, 21,
               "09/30/2029"),
    _mk_result("lease_term_yrs", "5.17", 0.88, 1, "sixty-two (62) months"),
    _mk_result("leased_rsf", "27,298", 0.94, 1, "27,298 square feet"),
    _mk_result("suite", "125", 0.95, 1, "Suite 125"),
    _mk_result("street_address", "4900 Jones Sausage Road, Suite 125", 0.92, 1, None),
    _mk_result("city", "Garner", 0.93, 1, "Garner, North Carolina"),
    _mk_result("state", "NC", 0.95, 1, "North Carolina"),
    _mk_result("property_name", "Beacon Commerce Park", 0.90, 1,
               "Beacon Commerce Park"),
    _mk_result("lease_guarantor", "None", 0.92, 24, "Intentionally reserved"),

    # === Financial - mix of correct/partial ===
    _mk_result("annual_base_rent", "$369,887.90", 0.90, 1, "$13.55 per SF"),
    _mk_result("future_rent_steps",
               "Yr1 $30,823.99/mo; Yr2 $32,056.95/mo; Yr3 $33,339.23/mo (4% annual escalator)",
               0.83, 21, "08/01/2024-07/31/2025"),
    _mk_result("security_deposit", "$41,642.30", 0.96, 1, "$41,642.30"),
    _mk_result("late_payment",
               "5% late fee after 7 days; first late in 12-month period waived with notice",
               0.89, 3, "five percent"),

    # === Reimbursements ===
    _mk_result("pro_rata", "10.46%", 0.95, 1, "10.46%"),
    _mk_result("re_taxes",
               "Tenant pays Proportionate Share of all Taxes monthly in estimated installments",
               0.88, 4, "Proportionate Share of all taxes"),
    _mk_result("utilities",
               "Tenant pays all utilities directly",
               0.91, 7, "Tenant shall pay for all water"),
    _mk_result("base_year", "None", 0.85, None, None),

    # === Critical ===
    _mk_result("permitted_use",
               "general office, warehouse and distribution uses; 2,000 SF outlet sales permitted",
               0.92, 5, "general office, warehouse and distribution"),
    _mk_result("renewal_options",
               "One 60-month renewal; 6-month notice; rent = greater of $17.15/SF or FMV",
               0.82, 21, "Renewal Option"),
    # Intentional failure example: extracted only part of the tiered holdover
    _mk_result("holdover",
               "150% of Monthly Base Rent",
               0.70, 3, "one hundred fifty percent"),
    _mk_result("relocation",
               "Landlord may relocate with 60 days' notice; reimburses moving costs",
               0.88, 13, "relocate Tenant"),
    _mk_result("purchase_option", "None", 0.90, None, None),

    # === Other ===
    _mk_result("allowance", "$10,000 Construction Allowance; 6-month deadline",
               0.92, 19, "$10,000"),
    _mk_result("parking",
               "Shared non-reserved parking; overnight vehicles allowed; 7-day max undriven",
               0.90, 5, "park on or utilize parking"),
    _mk_result("subordination",
               "Subordinate to existing/future mortgages; SNDA within 10 days; failure = Event of Default",
               0.88, 14, "subject and subordinate"),
    _mk_result("estoppel_certificate", "10 days; failure = Event of Default", 0.93, 14,
               "ten (10) days"),
    _mk_result("brokers", "Hartwell Realty and CBRE; Landlord pays", 0.90, 15,
               "Hartwell Realty and CBRE"),
    _mk_result("force_majeure",
               "Acts of God, strikes, government; rent obligations not excused",
               0.87, 15, "inclement weather"),
    # Another realistic miss — hallucinated page number
    _mk_result("move_out_conditions",
               "16-point checklist including HVAC cert, broom-clean, key return",
               0.85, 28, "Move-Out Checklist"),  # actual is on page 29

    # === Correct-None for retail gates ===
    _mk_result("percentage_rent", "None", 0.95, None, None),
    _mk_result("co_tenancy", "None", 0.95, None, None),
    _mk_result("continuous_operation", "None", 0.95, None, None),
    _mk_result("exclusive_use", "None", 0.90, None, None),
]


def main():
    print("Running harness with simulated v2.0 results on Sample 6...\n")

    report = evaluate_document(
        gt_doc=SAMPLE_6_GROUND_TRUTH,
        extracted_fields=SIMULATED_RESULTS,
        red_flags=[],
    )

    aggregate = build_aggregate_report(
        per_doc=[report],
        ground_truth_docs=[SAMPLE_6_GROUND_TRUTH],
    )

    md = report_to_markdown(aggregate)
    print(md)

    # Also write to file for inspection
    out = Path(__file__).parent / "demo_report.md"
    out.write_text(md)
    print(f"\nReport saved to {out}")


if __name__ == "__main__":
    main()
