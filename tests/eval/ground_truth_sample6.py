"""Ground truth for Sample 6 (HMBP-BCP Garner Appliance).

Hand-labeled from the 30-page lease. Used by harness.py to measure
extraction accuracy on this document. All values verified against the
source PDF; citations reference the section or Face Page.

This file also serves as a template for labeling the remaining 16 corpus
documents. Each field includes the difficulty score so per-difficulty
calibration can be computed.
"""
from tests.eval.harness import GroundTruthDocument, GroundTruthField


SAMPLE_6_GROUND_TRUTH = GroundTruthDocument(
    document_path="Sample 6.pdf",
    property_type="Industrial",
    document_type="base_lease",
    fields=[
        # ----- Basic Information -----
        GroundTruthField(
            field_id="tenant_name",
            expected_value="GARNER T.V. & APPLIANCES INC. (dba GARNER APPLIANCE & MATTRESS)",
            expected_page=1,
            expected_clause_snippet="GARNER T.V. & APPLIANCES INC.",
            difficulty=1,
        ),
        GroundTruthField(
            field_id="landlord_name",
            expected_value="HMBP - BCP LLC",
            expected_page=1,
            expected_clause_snippet="HMBP - BCP LLC",
            difficulty=1,
        ),
        GroundTruthField(
            field_id="lease_date",
            expected_value="August 2, 2024",
            expected_page=1,
            expected_clause_snippet="8/2/2024",
            date_days_tolerance=0,
            difficulty=1,
        ),
        GroundTruthField(
            field_id="original_lease_commencement_date",
            expected_value="August 1, 2024",
            expected_page=2,
            expected_clause_snippet="August 1, 2024",
            difficulty=2,
        ),
        GroundTruthField(
            field_id="lease_expiration_date",
            expected_value="September 30, 2029",
            expected_page=21,
            expected_clause_snippet="09/30/2029",
            date_days_tolerance=1,
            difficulty=4,
            notes="62 months from 8/1/2024, ends 9/30/2029; cross-verify Exhibit C",
        ),
        GroundTruthField(
            field_id="lease_term_yrs",
            expected_value="5.17",
            expected_page=1,
            difficulty=4,
            notes="62 months / 12",
        ),
        GroundTruthField(
            field_id="leased_rsf",
            expected_value="27,298",
            expected_page=1,
            expected_clause_snippet="27,298 square feet",
            difficulty=1,
        ),
        GroundTruthField(
            field_id="suite",
            expected_value="125",
            expected_page=1,
            expected_clause_snippet="Suite 125",
            difficulty=1,
        ),
        GroundTruthField(
            field_id="street_address",
            expected_value="4900 Jones Sausage Road, Suite 125",
            expected_page=1,
            difficulty=2,
        ),
        GroundTruthField(
            field_id="city",
            expected_value="Garner",
            expected_page=1,
            difficulty=1,
        ),
        GroundTruthField(
            field_id="state",
            expected_value="NC",
            expected_page=1,
            difficulty=1,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="property_name",
            expected_value="Beacon Commerce Park",
            expected_page=1,
            expected_clause_snippet="Beacon Commerce Park",
            difficulty=2,
        ),
        GroundTruthField(
            field_id="lease_guarantor",
            expected_value=None,          # "Intentionally reserved"
            expected_page=24,
            difficulty=2,
        ),

        # ----- Financial Clauses -----
        GroundTruthField(
            field_id="annual_base_rent",
            expected_value="$369,887.90",
            expected_page=1,
            expected_clause_snippet="$13.55 per SF",
            currency_pct_tolerance=0.01,
            difficulty=3,
            notes="$13.55/SF × 27,298 SF = $369,887.90",
        ),
        GroundTruthField(
            field_id="future_rent_steps",
            expected_value="Annual 4% escalator: Yr1 $30,823.99/mo ($13.55/SF); Yr2 $32,056.95/mo ($14.09/SF); Yr3 $33,339.23/mo ($14.66/SF); Yr4 $34,672.80/mo ($15.24/SF); Yr5 $36,059.71/mo ($15.85/SF); Yr6 $37,502.10/mo ($16.49/SF)",
            expected_page=21,
            expected_clause_snippet="08/01/2024-07/31/2025",
            difficulty=4,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="security_deposit",
            expected_value="$41,642.30",
            expected_page=1,
            expected_clause_snippet="$41,642.30",
            currency_pct_tolerance=0.0,
            difficulty=1,
        ),
        GroundTruthField(
            field_id="late_payment",
            expected_value="5% of payment after 7-day grace; first late in 12-month period waived with 3-business-day cure upon notice",
            expected_page=3,
            expected_clause_snippet="five percent",
            difficulty=2,
            partial_match_acceptable=True,
        ),

        # ----- Reimbursements -----
        GroundTruthField(
            field_id="pro_rata",
            expected_value="10.46%",
            expected_page=1,
            expected_clause_snippet="10.46%",
            difficulty=1,
        ),
        GroundTruthField(
            field_id="re_taxes",
            expected_value="Tenant pays Proportionate Share (10.46%) of all Taxes; paid monthly in 1/12 estimated installments; reconciled annually",
            expected_page=4,
            expected_clause_snippet="Proportionate Share of all taxes",
            difficulty=2,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="utilities",
            expected_value="Tenant pays directly for all utilities (water, sewer, electric, gas, telephone, data, sprinkler/fire protection)",
            expected_page=7,
            expected_clause_snippet="Tenant shall pay for all water",
            difficulty=2,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="base_year",
            expected_value=None,          # Net lease — no base year
            difficulty=2,
        ),

        # ----- Critical Clauses -----
        GroundTruthField(
            field_id="permitted_use",
            expected_value="general office, warehouse and distribution uses; up to 2,000 SF for inventory outlet sales",
            expected_page=5,
            expected_clause_snippet="general office, warehouse and distribution",
            difficulty=2,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="renewal_options",
            expected_value="One 60-month renewal option; notice 6 months before expiration; rent = greater of $17.15/SF or FMV; 4% annual escalator",
            expected_page=21,
            expected_clause_snippet="Renewal Option",
            difficulty=4,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="holdover",
            expected_value="Tiered: 150% MBR first 3 months if 90-day notice given; otherwise 200% from day 1; 200% thereafter",
            expected_page=3,
            expected_clause_snippet="one hundred fifty percent (150%)",
            difficulty=4,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="relocation",
            expected_value="Landlord may relocate with 60 days' notice to comparable space; reimburses reasonable moving costs; rent capped at pre-relocation if new space larger",
            expected_page=13,
            expected_clause_snippet="relocate Tenant",
            difficulty=3,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="purchase_option",
            expected_value=None,          # Not in this lease
            difficulty=2,
        ),

        # ----- Other Lease Clauses -----
        GroundTruthField(
            field_id="allowance",
            expected_value="$10,000 Construction Allowance; must be used within 6 months of Commencement Date",
            expected_page=19,
            expected_clause_snippet="$10,000",
            currency_pct_tolerance=0.0,
            difficulty=2,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="parking",
            expected_value="Shared non-reserved parking; overnight vehicles permitted; no undriven vehicles >7 consecutive days; no repair work on-site",
            expected_page=5,
            expected_clause_snippet="park on or utilize parking",
            difficulty=2,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="subordination",
            expected_value="Subordinate to existing and future mortgages; SNDA required within 10 days of Landlord's request; failure = Event of Default",
            expected_page=14,
            expected_clause_snippet="subject and subordinate",
            difficulty=3,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="estoppel_certificate",
            expected_value="10-day delivery window; failure = Event of Default",
            expected_page=14,
            expected_clause_snippet="ten (10) days following written notice",
            difficulty=2,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="brokers",
            expected_value="Hartwell Realty and CBRE; Landlord pays per separate agreement",
            expected_page=15,
            expected_clause_snippet="Hartwell Realty and CBRE",
            difficulty=2,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="force_majeure",
            expected_value="Excuses non-monetary performance for Acts of God, strikes, war, terrorism, government regulations; rent obligations remain absolute",
            expected_page=15,
            expected_clause_snippet="inclement weather",
            difficulty=3,
            partial_match_acceptable=True,
        ),
        GroundTruthField(
            field_id="move_out_conditions",
            expected_value="Detailed 16-point Exhibit G checklist: personal property/racking removal, HVAC service certification, lighting, plumbing, dock equipment, broom-clean, signage removal, keys returned",
            expected_page=29,
            expected_clause_snippet="Move-Out Checklist",
            difficulty=3,
            partial_match_acceptable=True,
        ),

        # ----- Correct-None cases (retail-only fields should NOT fire on industrial) -----
        GroundTruthField(
            field_id="percentage_rent",
            expected_value=None,
            difficulty=1,
            notes="Industrial lease — retail-only field should be gated",
        ),
        GroundTruthField(
            field_id="co_tenancy",
            expected_value=None,
            difficulty=1,
            notes="Industrial — retail-only field should be gated",
        ),
        GroundTruthField(
            field_id="continuous_operation",
            expected_value=None,
            difficulty=1,
        ),
        GroundTruthField(
            field_id="exclusive_use",
            expected_value=None,
            difficulty=1,
            notes="No exclusive-use provision in this industrial lease",
        ),
    ],
)

# Export as a simple data structure
ALL_GROUND_TRUTH = [SAMPLE_6_GROUND_TRUTH]


if __name__ == "__main__":
    print(f"Sample 6 ground truth: {len(SAMPLE_6_GROUND_TRUTH.fields)} fields labeled")
    by_diff = {}
    for f in SAMPLE_6_GROUND_TRUTH.fields:
        by_diff.setdefault(f.difficulty, 0)
        by_diff[f.difficulty] += 1
    for d in sorted(by_diff):
        print(f"  Difficulty {d}: {by_diff[d]} fields")
