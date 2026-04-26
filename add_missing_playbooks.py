"""Generate the 8 missing HIGH-severity playbooks identified in the
corpus coverage analysis.

Each playbook follows the same structure as existing compiled playbooks in
data/playbooks_compiled/. The keywords and question decision-trees are based
on the actual phrasing observed in the 17-lease corpus.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "data" / "playbooks_compiled"
OUT.mkdir(parents=True, exist_ok=True)


def _base_playbook(field_id: str, field_name: str, category: str,
                   overview: str, keywords: list[str],
                   questions: list[dict],
                   property_applicability: dict | None = None,
                   source_docx: str = "OTHER_LEASE_CLAUSES.docx") -> dict:
    return {
        "field_id": field_id,
        "field_name": field_name,
        "category": category,
        "output_type": "Text",
        "property_applicability": property_applicability or {
            "Retail": True, "Industrial": True, "Office": True,
        },
        "abstract_applicability": {},
        "source_docx": source_docx,
        "overview": overview,
        "preliminary": [
            "OTHER LEASE CLAUSES – MASTER ABSTRACTION GUIDE",
            "General Applicability: All leases (Office, Retail and Industrial).",
            "Summary", "Definitions", "Sections", "Addenda & Amendments",
            "If conflict exists → Amendment controls.",
        ],
        "questions": questions,
        "keywords": keywords,
        "summary_keywords": keywords[:8],
        "amendment_controls": True,
        "depends_on": [],
    }


# ---------------------------------------------------------------------------
# 1. LATE PAYMENT — late fee % + grace period
# ---------------------------------------------------------------------------
late_payment = _base_playbook(
    field_id="late_payment",
    field_name="Late Payment",
    category="Financial Clauses",
    source_docx="FINANCIAL_TERMS.docx",
    overview=(
        "A late payment clause specifies the consequences when Tenant fails to pay "
        "Rent by the due date. It typically includes (a) a grace period before the "
        "payment is considered late, (b) a late charge expressed as a flat fee or "
        "percentage of the unpaid amount (most commonly 5%), and (c) an interest "
        "rate applied to delinquent amounts. Many leases waive the first late "
        "charge per 12-month period if Tenant cures within a short window after "
        "written notice."
    ),
    keywords=[
        "Late Payment Charge", "Late Charge", "Late Fee", "Late Payment",
        "grace period", "past due", "delinquent", "overdue",
        "5% of such payment", "five percent", "late charge equal to",
        "not received within", "days of the date when",
        "dishonored", "returned check", "insufficient funds",
        "default interest rate", "interest on overdue",
    ],
    questions=[
        {
            "id": "Q1", "priority": 1, "condition_type": "Definition Based",
            "question_text": (
                "Does the lease contain a Late Payment Charge or late fee clause "
                "(keywords: 'late charge', 'late payment charge', 'late fee', or "
                "a stated percentage of delinquent amount)?"
            ),
            "extraction_hint": "Extract full clause including: (a) grace period in days, (b) late fee amount or %, (c) any first-late-waiver provision, (d) any default interest rate on overdue sums.",
            "output_type": "Text", "search_scope": "all",
            "keywords": [
                "late charge", "late payment charge", "late fee",
                "percent of such", "grace period", "days of the date",
            ],
            "yes_branch": {"type": "extract", "goto": "Q2", "literal": None, "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "None", "also_extract": False},
            "red_flag": None,
            "notes": "Default location: Section 3 (HMBP-BCP leases) or Paragraph 4 (ProLogis leases).",
        },
        {
            "id": "Q2", "priority": 2, "condition_type": "Amount Based",
            "question_text": "What is the late fee amount or percentage?",
            "extraction_hint": "Extract the dollar amount or percentage (e.g., '5%' or '$100').",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["percent of such", "% of", "late charge equal to"],
            "yes_branch": {"type": "extract", "goto": "Q3", "literal": None, "also_extract": True},
            "no_branch":  {"type": "none", "goto": None, "literal": None, "also_extract": False},
            "red_flag": None, "notes": None,
        },
        {
            "id": "Q3", "priority": 3, "condition_type": "Period Based",
            "question_text": "What is the grace period (number of days after due date before late charge applies)?",
            "extraction_hint": "Extract the number of days, e.g., '7 days', '10 days'.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["within", "days of the date", "days after", "grace period"],
            "yes_branch": {"type": "extract", "goto": None, "literal": None, "also_extract": True},
            "no_branch":  {"type": "none", "goto": None, "literal": None, "also_extract": False},
            "red_flag": None, "notes": None,
        },
    ],
)

# ---------------------------------------------------------------------------
# 2. MOVE-OUT CONDITIONS — surrender requirements + restoration
# ---------------------------------------------------------------------------
move_out_conditions = _base_playbook(
    field_id="move_out_conditions",
    field_name="Move-Out Conditions",
    category="Other Lease Clauses",
    overview=(
        "A move-out conditions clause specifies the physical state in which Tenant "
        "must deliver the Premises upon expiration or earlier termination of the "
        "Lease. These requirements typically include: (a) broom-clean condition, "
        "(b) HVAC service certification by a qualified contractor, (c) repair of "
        "all damage beyond ordinary wear and tear, (d) removal of Tenant-installed "
        "equipment, signage, and wiring, (e) operable condition of lighting, "
        "plumbing, dock doors/levelers, (f) patching of wall penetrations, and "
        "(g) return of all keys and access devices. Leases often include a detailed "
        "exhibit (e.g., 'Move-Out Checklist') enumerating 10-20 specific items."
    ),
    keywords=[
        "Move-Out Conditions", "Move Out Checklist", "Surrender of Premises",
        "Surrender", "surrender the Premises", "deliver the Premises",
        "broom clean", "broom-clean", "broom-swept",
        "ordinary wear and tear", "reasonable wear and tear",
        "HVAC certification", "HVAC inspection", "preventive maintenance",
        "dock doors", "dock levelers", "truck door",
        "remove all signs", "remove Tenant's Property", "remove racking",
        "restoration", "restore the Premises",
        "tenant improvements", "leasehold improvements",
        "return all keys", "access cards", "key cards",
        "patched", "repair any damage", "grind down", "anchors flush",
    ],
    questions=[
        {
            "id": "Q1", "priority": 1, "condition_type": "Definition Based",
            "question_text": (
                "Does the lease contain a move-out conditions, surrender, or "
                "'Move-Out Checklist' clause specifying the condition in which "
                "Tenant must deliver the Premises?"
            ),
            "extraction_hint": "Extract complete move-out clause including surrender standard, HVAC/dock equipment requirements, removal obligations, and any exhibit reference (e.g., 'See Exhibit G' or 'See Addendum 9').",
            "output_type": "Text", "search_scope": "all",
            "keywords": [
                "surrender", "broom clean", "move-out", "move out",
                "condition as received", "deliver the Premises",
            ],
            "yes_branch": {"type": "extract", "goto": "Q2", "literal": None, "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "None (no explicit move-out provisions)", "also_extract": False},
            "red_flag": None,
            "notes": "Common locations: Section 4 (HMBP-BCP), Paragraph 21 + Addendum 9 (ProLogis), dedicated 'Move-Out Checklist' exhibit.",
        },
        {
            "id": "Q2", "priority": 2, "condition_type": "Definition Based",
            "question_text": "Is an HVAC certification or maintenance contract required at move-out?",
            "extraction_hint": "Extract the HVAC-related move-out requirement.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["HVAC", "heating, ventilation", "air conditioning", "service contract", "certification"],
            "yes_branch": {"type": "extract", "goto": "Q3", "literal": None, "also_extract": True},
            "no_branch":  {"type": "none", "goto": "Q3", "literal": None, "also_extract": False},
            "red_flag": None, "notes": None,
        },
        {
            "id": "Q3", "priority": 3, "condition_type": "Definition Based",
            "question_text": "Does Tenant have removal or restoration obligations for alterations or improvements?",
            "extraction_hint": "Extract the removal/restoration obligation.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["remove all", "restore the Premises", "Tenant-Made Alterations", "leasehold improvements", "Tenant's Property"],
            "yes_branch": {"type": "extract", "goto": None, "literal": None, "also_extract": True},
            "no_branch":  {"type": "none", "goto": None, "literal": None, "also_extract": False},
            "red_flag": None, "notes": None,
        },
    ],
)

# ---------------------------------------------------------------------------
# 3. NOTICES — notice address + delivery method
# ---------------------------------------------------------------------------
notices = _base_playbook(
    field_id="notices",
    field_name="Notices",
    category="Other Lease Clauses",
    overview=(
        "A notices clause specifies how formal written communications between "
        "Landlord and Tenant must be sent to be legally effective. It typically "
        "defines: (a) the notice addresses for each party (and any required "
        "'with a copy to' addresses for legal counsel), (b) permitted delivery "
        "methods (certified mail, overnight courier, hand delivery), and (c) "
        "when notice is deemed given (upon receipt, upon delivery, after a fixed "
        "period). Failed or defective notice can invalidate default declarations "
        "and cure periods, so this is a high-impact operational clause."
    ),
    keywords=[
        "Notices", "notice address", "written notice",
        "certified mail", "return receipt requested", "registered mail",
        "overnight courier", "Federal Express", "FedEx", "UPS",
        "hand delivery", "hand-delivered",
        "postage prepaid", "postage pre-paid",
        "deemed given", "deemed delivered", "deemed received",
        "Attn:", "Attention:", "with a copy to",
        "Landlord's Notice Address", "Tenant's Notice Address",
    ],
    questions=[
        {
            "id": "Q1", "priority": 1, "condition_type": "Definition Based",
            "question_text": "Does the lease contain a notices clause specifying how formal communications must be delivered?",
            "extraction_hint": "Extract full notices clause including: (a) Landlord notice address, (b) Tenant notice address, (c) any 'with a copy to' addresses, (d) permitted delivery methods, (e) when notice is deemed effective.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["notices", "notice address", "certified mail", "overnight courier", "deemed given"],
            "yes_branch": {"type": "extract", "goto": "Q2", "literal": None, "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "None", "also_extract": False},
            "red_flag": None,
            "notes": "Common locations: Section 28.1 (HMBP-BCP leases), Paragraph 37(c) (ProLogis leases).",
        },
        {
            "id": "Q2", "priority": 2, "condition_type": "Location Based",
            "question_text": "What is the Landlord's notice address (including attention line)?",
            "extraction_hint": "Extract the full Landlord notice address block.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["To Landlord at", "Landlord's Notice Address", "LANDLORD'S NOTICE"],
            "yes_branch": {"type": "extract", "goto": "Q3", "literal": None, "also_extract": True},
            "no_branch":  {"type": "none", "goto": "Q3", "literal": None, "also_extract": False},
            "red_flag": None, "notes": None,
        },
        {
            "id": "Q3", "priority": 3, "condition_type": "Location Based",
            "question_text": "What is the Tenant's notice address (including attention line)?",
            "extraction_hint": "Extract the full Tenant notice address block.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["To Tenant at", "Tenant's Notice Address", "TENANT'S NOTICE"],
            "yes_branch": {"type": "extract", "goto": None, "literal": None, "also_extract": True},
            "no_branch":  {"type": "none", "goto": None, "literal": None, "also_extract": False},
            "red_flag": None, "notes": None,
        },
    ],
)

# ---------------------------------------------------------------------------
# 4. INDEMNIFICATION — mutual indemnities
# ---------------------------------------------------------------------------
indemnification = _base_playbook(
    field_id="indemnification",
    field_name="Indemnification",
    category="Other Lease Clauses",
    overview=(
        "An indemnification clause allocates legal and financial risk between "
        "Landlord and Tenant for third-party claims arising from use of the "
        "Premises. A typical clause contains: (a) Tenant's obligation to "
        "indemnify Landlord for claims arising from Tenant's use, occupancy, or "
        "acts/omissions of its agents, and (b) often, Landlord's reciprocal "
        "obligation to indemnify Tenant for claims arising from Landlord's "
        "negligence or willful misconduct. The clause works in tandem with the "
        "parties' insurance obligations — in most cases the indemnitor's insurance "
        "is the first source of recovery."
    ),
    keywords=[
        "Indemnification", "Indemnity", "indemnify",
        "indemnify, defend, and hold harmless", "indemnify and hold harmless",
        "defend and hold harmless", "save and hold harmless",
        "against any and all losses, liabilities, damages",
        "costs and expenses", "attorneys' fees", "attorney fees",
        "claims by third parties", "third-party claims",
        "except for the negligence of Landlord",
        "mutual indemnification",
    ],
    questions=[
        {
            "id": "Q1", "priority": 1, "condition_type": "Definition Based",
            "question_text": "Does the lease contain indemnification obligations?",
            "extraction_hint": "Extract the full indemnification clause(s), identifying separately: (a) Tenant-indemnifies-Landlord, (b) Landlord-indemnifies-Tenant (if mutual), (c) any carve-outs or exclusions.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["indemnify", "indemnification", "hold harmless", "defend and hold"],
            "yes_branch": {"type": "extract", "goto": "Q2", "literal": None, "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "None", "also_extract": False},
            "red_flag": "Flag if Tenant indemnifies Landlord for Landlord's own negligence (unusual and unfavorable to Tenant).",
            "notes": "Common locations: Section 15 + 7.3 (HMBP-BCP), Paragraph 18 (ProLogis).",
        },
        {
            "id": "Q2", "priority": 2, "condition_type": "Yes/No",
            "question_text": "Is the indemnification mutual (both parties indemnify each other)?",
            "extraction_hint": "Determine if Landlord also indemnifies Tenant. If yes, extract the Landlord-to-Tenant indemnity.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["Landlord agrees to indemnify", "Landlord shall indemnify", "Landlord hereby indemnifies"],
            "yes_branch": {"type": "extract", "goto": None, "literal": "Mutual indemnification", "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "One-way (Tenant indemnifies Landlord only)", "also_extract": False},
            "red_flag": None, "notes": None,
        },
    ],
)

# ---------------------------------------------------------------------------
# 5. RULES AND REGULATIONS
# ---------------------------------------------------------------------------
rules_and_regulations = _base_playbook(
    field_id="rules_and_regulations",
    field_name="Rules and Regulations",
    category="Other Lease Clauses",
    overview=(
        "A Rules and Regulations clause governs Tenant's day-to-day behavior at "
        "the Premises and Project. It typically establishes rules for common "
        "areas, parking, signage, waste disposal, noise, odors, pets, smoking, "
        "and hours of operation. Rules are almost always attached as an exhibit "
        "and Landlord typically reserves the right to modify them so long as "
        "changes are uniformly enforced. If rules conflict with lease terms, "
        "the lease generally controls."
    ),
    keywords=[
        "Rules and Regulations", "Rules & Regulations",
        "uniformly enforced", "uniformly applied",
        "common areas", "ingress and egress",
        "outside storage", "obstruct", "nuisance",
        "modify the Rules", "amend the Rules",
        "conflict between said rules", "conflict.+rules",
    ],
    questions=[
        {
            "id": "Q1", "priority": 1, "condition_type": "Definition Based",
            "question_text": "Does the lease include Rules and Regulations (either in the body or as an exhibit)?",
            "extraction_hint": "Identify whether R&R are: (a) in body of lease, (b) in an exhibit/addendum (note which one), (c) by reference to a separate document. Summarize key restrictions, and note Landlord's right to modify.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["rules and regulations", "rules & regulations"],
            "yes_branch": {"type": "extract", "goto": None, "literal": None, "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "None", "also_extract": False},
            "red_flag": None,
            "notes": "Common locations: Section 6 + Exhibit D (HMBP-BCP), Paragraph 31 + Rules exhibit (ProLogis).",
        },
    ],
)

# ---------------------------------------------------------------------------
# 6. ESTOPPEL CERTIFICATE
# ---------------------------------------------------------------------------
estoppel_certificate = _base_playbook(
    field_id="estoppel_certificate",
    field_name="Estoppel Certificate",
    category="Other Lease Clauses",
    overview=(
        "An estoppel certificate clause obligates Tenant to execute, upon "
        "Landlord's request, a certificate confirming key facts about the lease "
        "(e.g., commencement/expiration dates, rent paid to date, no existing "
        "defaults, no prepaid rent beyond one month). Estoppels are used by "
        "Landlord's lenders and prospective purchasers in financing and sale "
        "transactions. The clause typically specifies: (a) a response deadline "
        "(commonly 10-20 days), (b) the certificate contents, and (c) whether "
        "failure to deliver constitutes an Event of Default or deemed-approval."
    ),
    keywords=[
        "Estoppel Certificate", "estoppel",
        "execute and deliver", "execute an estoppel",
        "within ten (10) days", "within twenty (20) days",
        "ratifying this Lease", "certifying",
        "full force and effect", "no defenses or offsets",
        "lender or prospective lender", "material inducement",
        "deemed given", "deemed to have accepted",
    ],
    questions=[
        {
            "id": "Q1", "priority": 1, "condition_type": "Definition Based",
            "question_text": "Does the lease require Tenant to execute estoppel certificates upon Landlord's request?",
            "extraction_hint": "Extract the full estoppel clause, including: (a) response deadline, (b) required contents, (c) consequence of failure to deliver (Event of Default? Deemed-approval?).",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["estoppel certificate", "estoppel"],
            "yes_branch": {"type": "extract", "goto": "Q2", "literal": None, "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "None", "also_extract": False},
            "red_flag": None,
            "notes": "Common locations: Section 27.2 (HMBP-BCP), Paragraph 29 (ProLogis).",
        },
        {
            "id": "Q2", "priority": 2, "condition_type": "Period Based",
            "question_text": "What is the response deadline (in days)?",
            "extraction_hint": "Extract the response deadline and identify the cure consequence.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["within", "days following written notice", "days of request"],
            "yes_branch": {"type": "extract", "goto": None, "literal": None, "also_extract": True},
            "no_branch":  {"type": "none", "goto": None, "literal": None, "also_extract": False},
            "red_flag": None, "notes": None,
        },
    ],
)

# ---------------------------------------------------------------------------
# 7. FORCE MAJEURE
# ---------------------------------------------------------------------------
force_majeure = _base_playbook(
    field_id="force_majeure",
    field_name="Force Majeure",
    category="Other Lease Clauses",
    overview=(
        "A force majeure clause excuses or delays a party's performance when "
        "performance is prevented by causes beyond that party's reasonable "
        "control (e.g., acts of God, war, terrorism, strikes, government "
        "regulations, pandemics, natural disasters). Key distinctions: (a) "
        "which events qualify, (b) which party may invoke the clause (often "
        "Landlord-only, sometimes mutual), (c) whether rent obligations are "
        "excused (rarely — most leases specify that monetary obligations are "
        "NOT excused by force majeure even when other obligations are), and "
        "(d) required notice and mitigation obligations."
    ),
    keywords=[
        "Force Majeure", "force majeure",
        "acts of God", "acts of god",
        "strikes", "lockouts", "labor disputes",
        "war", "terrorism", "terrorist acts",
        "riots", "civil commotion", "civil disturbance",
        "government regulations", "government restrictions",
        "inability to obtain labor or materials",
        "beyond the reasonable control", "beyond such party's control",
        "pandemic", "epidemic",
    ],
    questions=[
        {
            "id": "Q1", "priority": 1, "condition_type": "Definition Based",
            "question_text": "Does the lease contain a force majeure clause?",
            "extraction_hint": "Extract the full force majeure clause. Note: (a) qualifying events, (b) which party(ies) may invoke it, (c) whether payment obligations are excused or remain absolute.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["force majeure", "acts of God", "beyond the reasonable control"],
            "yes_branch": {"type": "extract", "goto": "Q2", "literal": None, "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "None", "also_extract": False},
            "red_flag": None,
            "notes": "Common locations: Section 28.15 (HMBP-BCP), Paragraph 33 (ProLogis).",
        },
        {
            "id": "Q2", "priority": 2, "condition_type": "Yes/No",
            "question_text": "Are rent/monetary obligations excused by force majeure?",
            "extraction_hint": "Most leases explicitly state that monetary obligations are NOT excused. Confirm.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["obligation to pay", "absolute and unconditional", "shall remain", "not be delayed or excused"],
            "yes_branch": {"type": "literal", "goto": None, "literal": "Yes — rent obligations excused (unusual/tenant-favorable)", "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "No — rent obligations remain absolute", "also_extract": True},
            "red_flag": None, "notes": None,
        },
    ],
)

# ---------------------------------------------------------------------------
# 8. BROKERS
# ---------------------------------------------------------------------------
brokers = _base_playbook(
    field_id="brokers",
    field_name="Brokers",
    category="Other Lease Clauses",
    overview=(
        "A brokers clause identifies the real-estate brokers involved in the "
        "lease transaction and allocates responsibility for broker commissions. "
        "It typically: (a) names the brokers representing each party (if any), "
        "(b) identifies who is responsible for paying each commission (usually "
        "Landlord), and (c) contains mutual indemnities for undisclosed broker "
        "claims. Some leases include a commission schedule by reference to a "
        "separate listing agreement."
    ),
    keywords=[
        "Brokers", "Broker", "brokerage commission",
        "commission to the broker", "commission to Broker",
        "no broker", "no brokers",
        "indemnify and hold the other party harmless.+broker",
        "listing agent", "representing Tenant", "representing Landlord",
        "dual agency", "separate agreement between Landlord and",
    ],
    questions=[
        {
            "id": "Q1", "priority": 1, "condition_type": "Definition Based",
            "question_text": "Does the lease identify any brokers involved in the transaction?",
            "extraction_hint": "Extract: (a) named brokers (Landlord's broker, Tenant's broker, or dual agent), (b) which party pays each commission, (c) any indemnities.",
            "output_type": "Text", "search_scope": "all",
            "keywords": ["brokers", "broker", "commission"],
            "yes_branch": {"type": "extract", "goto": None, "literal": None, "also_extract": True},
            "no_branch":  {"type": "literal", "goto": None, "literal": "No broker involved in this transaction", "also_extract": False},
            "red_flag": None,
            "notes": "Common locations: Section 28.12 (HMBP-BCP), Paragraph 36 (ProLogis). Also check Face Page for broker name.",
        },
    ],
)

# ---------------------------------------------------------------------------
# Write all 8 playbooks and update the index
# ---------------------------------------------------------------------------

NEW_PLAYBOOKS = [
    late_payment, move_out_conditions, notices, indemnification,
    rules_and_regulations, estoppel_certificate, force_majeure, brokers,
]


def write_all():
    print(f"Writing {len(NEW_PLAYBOOKS)} new playbooks to {OUT}")
    new_files = []
    for pb in NEW_PLAYBOOKS:
        path = OUT / f"{pb['field_id']}.json"
        if path.exists():
            print(f"  SKIP (exists): {path.name}")
            continue
        with open(path, "w") as f:
            json.dump(pb, f, indent=2)
        new_files.append(pb)
        print(f"  ✓ {path.name}")

    # Update the index
    idx_path = OUT / "_index.json"
    with open(idx_path) as f:
        index = json.load(f)

    existing_ids = {p["field_id"] for p in index["playbooks"]}
    for pb in new_files:
        if pb["field_id"] in existing_ids:
            continue
        index["playbooks"].append({
            "field_id":       pb["field_id"],
            "field_name":     pb["field_name"],
            "category":       pb["category"],
            "source_docx":    pb["source_docx"],
            "question_count": len(pb["questions"]),
            "keyword_count":  len(pb["keywords"]),
            "output_type":    pb["output_type"],
            "file":           f"{pb['field_id']}.json",
        })

    index["count"] = len(index["playbooks"])
    index["playbooks"].sort(key=lambda p: p["field_id"])

    with open(idx_path, "w") as f:
        json.dump(index, f, indent=2)

    print(f"\nIndex updated: now {index['count']} playbooks total")


if __name__ == "__main__":
    write_all()
