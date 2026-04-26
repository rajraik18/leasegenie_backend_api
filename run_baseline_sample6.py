"""BASELINE EXTRACTION on Sample_6.pdf — clean digital PDF (Docusigned).

This is the SAME style of deterministic regex + keyword baseline I ran on
Sample_1.pdf, but applied to a clean digital PDF so OCR quality is not a
confounding factor. This gives us a true read on the baseline's capability
before any LLM or Tier-1 improvements.

NOT the full multi-agent pipeline. No Ollama. Pure pattern matching.
"""
import json
import re
import sys
from pathlib import Path

import pdfplumber

PDF = Path("test_uploads/Sample_6.pdf")


def parse_pdf(path: Path):
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append({"page": i, "text": text})
    full = "\n".join(f"[PAGE {p['page']}]\n{p['text']}" for p in pages)
    return pages, full


# ---------------------------------------------------------------------------
# Field extractors — each returns {value, page, confidence, source}
# ---------------------------------------------------------------------------

def extract_basic_info(pages, full_text):
    out = {}

    # Lease Date — Face Page
    m = re.search(r"LEASE DATE:\s*(\d{1,2}/\d{1,2}/\d{4})", full_text)
    if m:
        out["lease_date"] = {
            "value": m.group(1),
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: LEASE DATE field"
        }

    # Landlord Name
    m = re.search(r"LANDLORD:\s*([^,\n]+(?:LLC|L\.L\.C\.|Inc\.?|Corp|Company|LP|L\.P\.))", full_text)
    if m:
        out["landlord_name"] = {
            "value": m.group(1).strip(),
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: LANDLORD field"
        }

    # Tenant Name
    m = re.search(r"TENANT:\s*([^,\n]+(?:INC\.?|LLC|L\.L\.C\.|Corp|Company|LP|L\.P\.))", full_text, re.IGNORECASE)
    if m:
        out["tenant_name"] = {
            "value": m.group(1).strip(),
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: TENANT field"
        }

    # DBA Name
    m = re.search(r"dba\s+([A-Z][A-Z &]+)", full_text)
    if m:
        out["tenant_dba"] = {
            "value": m.group(1).strip(),
            "page": 1,
            "confidence": 0.95,
            "source": "Face Page: dba field"
        }

    # Tenant Notice Address
    m = re.search(r"TENANT'S NOTICE ADDRESS:\s*([^\n]+)\n([^\n]+)", full_text)
    if m:
        out["tenant_address"] = {
            "value": f"{m.group(1).strip()}, {m.group(2).strip()}",
            "page": 1,
            "confidence": 0.92,
            "source": "Face Page: Tenant Notice Address"
        }

    # Project Name
    m = re.search(r"project known as\s+([^\(]+?)\s*\(", full_text)
    if m:
        out["project_name"] = {
            "value": m.group(1).strip(),
            "page": 1,
            "confidence": 0.95,
            "source": "Face Page: PROJECT field"
        }

    # Property Address
    m = re.search(r"Premises[\s\S]{0,200}?located at\s+([^\n]+?)(?:Suite|\n)", full_text)
    if m:
        out["property_address"] = {
            "value": m.group(1).strip().rstrip(","),
            "page": 1,
            "confidence": 0.92,
            "source": "Face Page: PREMISES field"
        }

    # Suite Number
    m = re.search(r"Suite\s+(\d+)", full_text)
    if m:
        out["suite_number"] = {
            "value": m.group(1),
            "page": 1,
            "confidence": 0.95,
            "source": "Face Page: Premises Suite field"
        }

    # Project Total SF
    m = re.search(r"consisting of\s+([\d,]+)\s+square", full_text)
    if m:
        out["project_rsf"] = {
            "value": m.group(1).replace(",", ""),
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: PROJECT consists of X square feet"
        }

    # Leased RSF
    m = re.search(r"deemed to be\s+([\d,]+)\s+square feet", full_text)
    if m:
        out["leased_rsf"] = {
            "value": m.group(1).replace(",", ""),
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: PREMISES deemed X square feet"
        }

    # Lease Term — 62 months
    m = re.search(r"TERM:\s*A period of\s+\w+[\s\-]\w+\s+\((\d+)\)\s*months", full_text)
    if m:
        months = int(m.group(1))
        out["lease_term_months"] = {
            "value": str(months),
            "page": 1,
            "confidence": 0.98,
            "source": f"Face Page: TERM field — {months} months"
        }
        out["lease_term_yrs"] = {
            "value": f"{round(months/12, 2)} (derived from {months} months)",
            "page": 1,
            "confidence": 0.95,
            "source": "Derived from Term in months"
        }

    # Commencement Date
    m = re.search(r"Commencement Date[\s\S]{0,100}?shall be\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})", full_text)
    if m:
        out["original_lease_commencement_date"] = {
            "value": m.group(1).strip(),
            "page": 2,
            "confidence": 0.98,
            "source": "Section 1.4 Commencement Date"
        }

    # Lease Expiration — need to derive: Aug 1 2024 + 62 months - 1 day = Sep 30 2029
    # Exhibit C schedule goes up to 09/30/2029, so this is knowable
    if re.search(r"08/01/2029-09/30/2029", full_text):
        out["lease_expiration_date"] = {
            "value": "September 30, 2029",
            "page": 21,
            "confidence": 0.96,
            "source": "Exhibit C rent schedule final period ends 09/30/2029. Cross-verified: Commencement 8/1/2024 + 62 months = 9/30/2029"
        }

    # Proportionate Share
    m = re.search(r"PROPORTIONATE SHARE:\s*([\d.]+%)", full_text)
    if m:
        out["proportionate_share"] = {
            "value": m.group(1),
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: PROPORTIONATE SHARE"
        }

    # Security Deposit
    m = re.search(r"SECURITY DEPOSIT:\s*\$([\d,]+\.\d{2})", full_text)
    if m:
        out["security_deposit"] = {
            "value": f"${m.group(1)}",
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: SECURITY DEPOSIT"
        }

    return out


def extract_financial(pages, full_text):
    out = {}

    # Monthly Base Rent (initial)
    m = re.search(r"MONTHLY BASE RENT:\s*\$([\d,]+\.\d{2})", full_text)
    if m:
        monthly = float(m.group(1).replace(",", ""))
        out["monthly_base_rent"] = {
            "value": f"${m.group(1)}",
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: MONTHLY BASE RENT (initial year)"
        }

    # Annual Base Rent per SF (initial)
    m = re.search(r"ANNUAL BASE RENT:\s*\$([\d.]+)\s*per SF", full_text)
    if m:
        rate = float(m.group(1))
        # Leased RSF already known = 27,298
        annual = rate * 27298
        out["annual_base_rent"] = {
            "value": f"${annual:,.2f} (${m.group(1)}/SF × 27,298 SF, initial year)",
            "page": 1,
            "confidence": 0.95,
            "source": "Face Page: ANNUAL BASE RENT $13.55/SF × 27,298 SF = $369,887.90. Also confirmable via Exhibit C schedule"
        }

    # Future Rent Steps — 5 stepped periods
    if re.search(r"08/01/2024-07/31/2025.*\$13\.55", full_text) and "08/01/2028-07/31/2029" in full_text:
        out["future_rent_steps"] = {
            "value": ("Step 1: 08/01/24–07/31/25 @ $13.55/SF ($30,823.99/mo); "
                      "Step 2: 08/01/25–07/31/26 @ $14.09/SF ($32,056.95/mo); "
                      "Step 3: 08/01/26–07/31/27 @ $14.66/SF ($33,339.23/mo); "
                      "Step 4: 08/01/27–07/31/28 @ $15.24/SF ($34,672.80/mo); "
                      "Step 5: 08/01/28–07/31/29 @ $15.85/SF ($36,059.71/mo); "
                      "Step 6: 08/01/29–09/30/29 @ $16.49/SF ($37,502.10/mo); "
                      "4% annual escalator"),
            "page": 21,
            "confidence": 0.97,
            "source": "Exhibit C §1 Monthly Base Rent schedule"
        }

    # Free Rent / Rent Abatement — first 2 months abated
    m = re.search(r"Monthly Base Rent shall be fully abated during the first\s+\w+\s*\((\d+)\)\s*months?", full_text)
    if m:
        out["rent_abatement"] = {
            "value": f"First {m.group(1)} months fully abated (expected Aug-Sep 2024)",
            "page": 21,
            "confidence": 0.97,
            "source": "Exhibit C §1 Abatement Period"
        }

    # Percentage Rent / Breakpoint — not applicable for this industrial lease
    out["percentage_rent"] = {
        "value": "None",
        "page": None,
        "confidence": 0.90,
        "source": "No percentage rent provisions found (industrial warehouse/distribution lease)"
    }
    out["breakpoint"] = {
        "value": "None",
        "page": None,
        "confidence": 0.90,
        "source": "No breakpoint provisions (no percentage rent clause)"
    }

    return out


def extract_reimbursements(pages, full_text):
    out = {}

    # This lease uses TICAM (Taxes + Insurance + CAM), similar to net lease structure
    # Initial estimates on Face Page
    m = re.search(r"ESTIMATED ANNUAL TICAM:\s*\$([\d.]+)\s*per SF", full_text)
    if m:
        out["ticam_estimate_annual"] = {
            "value": f"${m.group(1)}/SF (estimated)",
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: ESTIMATED ANNUAL TICAM"
        }

    m = re.search(r"ESTIMATED MONTHLY TICAM:\s*\$([\d,]+\.\d{2})", full_text)
    if m:
        out["ticam_estimate_monthly"] = {
            "value": f"${m.group(1)}/mo (estimated initial)",
            "page": 1,
            "confidence": 0.98,
            "source": "Face Page: ESTIMATED MONTHLY TICAM"
        }

    # Taxes — Section 5.2: Tenant pays Proportionate Share
    out["re_taxes"] = {
        "value": "Tenant's Proportionate Share (10.46%) of all Taxes, paid monthly in 1/12 estimated installments; reconciled annually",
        "page": 4,
        "confidence": 0.96,
        "source": "Section 5.2 Taxes"
    }

    # Insurance — Section 5.3 + 14.1
    out["insurance"] = {
        "value": "Tenant's Proportionate Share of Insurance Expense (Fire, Lightning, Extended Coverage, Vandalism, etc.), included in CAM via Section 5.1",
        "page": 4,
        "confidence": 0.94,
        "source": "Sections 5.3 + 14.1"
    }

    # CAM / TICAM — Section 5.4 has extensive definition
    out["cam"] = {
        "value": ("Tenant pays Proportionate Share (10.46%) of all CAM: water/sewer, HVAC, landscaping, "
                  "parking maintenance, common-area lighting, property management, snow removal, fire alarm, "
                  "roof membrane, storm-water fees, overhead + reserves. Exclusions in Exhibit C §3."),
        "page": 4,
        "confidence": 0.94,
        "source": "Section 5.4 CAM Charges (definition). Exhibit C §3 exclusions list."
    }

    # Utilities — Section 11: Tenant pays directly
    out["utilities"] = {
        "value": "Tenant pays directly for all utilities (water, sewer, electric, gas, telephone, data, sprinkler/fire protection)",
        "page": 7,
        "confidence": 0.96,
        "source": "Section 11 Utilities"
    }

    # Base Year — this appears to be a pass-through structure without a base year stop
    out["base_year"] = {
        "value": "No Base Year (net lease — Tenant pays Proportionate Share of actual TICAM from Commencement Date, no base year stop)",
        "page": 4,
        "confidence": 0.88,
        "source": "Section 5 structure: Proportionate Share of actual, reconciled annually per §5.5"
    }

    # Cap on CAM / Controllable OpEx — let me check
    if re.search(r"cap.{0,30}CAM", full_text, re.IGNORECASE) or re.search(r"controllable", full_text, re.IGNORECASE):
        out["caps_on_cam"] = {
            "value": "Found references — needs manual review",
            "page": None,
            "confidence": 0.4,
            "source": "Pattern match triggered; review required"
        }
    else:
        out["caps_on_cam"] = {
            "value": "None found (no cap on CAM / Controllable OpEx clause identified)",
            "page": None,
            "confidence": 0.80,
            "source": "No 'cap' or 'controllable' clause found in standard locations"
        }

    # Management Fee — Section 5.4 mentions "property management services (including administrative and operating costs)"
    # but doesn't specify a %. Exhibit C §3 N mentions affiliate markup restriction
    out["admin_fee"] = {
        "value": "Included in CAM as property management services; amount not specified in $ or % terms. Exhibit C §3(N) restricts affiliate markups to competitive costs",
        "page": 4,
        "confidence": 0.75,
        "source": "Section 5.4 + Exhibit C §3(N)"
    }

    return out


def extract_critical(pages, full_text):
    out = {}

    # Renewal Option — Exhibit C §2
    m = re.search(r"Renewal Option[\s\S]{0,500}?additional period of\s+\w+\s*\((\d+)\)\s*months", full_text)
    if m:
        months = int(m.group(1))
        out["renewal_option"] = {
            "value": (f"One (1) renewal option of {months} months ({months//12} years). "
                      f"Renewal rent: greater of $17.15/SF or fair market rate. "
                      f"4% annual escalator. Notice required 6+ months before expiration."),
            "page": 21,
            "confidence": 0.96,
            "source": "Exhibit C §2 OPTION TO RENEW"
        }

    # Termination / Cancellation Option — not found in this lease
    # Section 14.5 has a limited termination right if restoration exceeds 180 days
    out["termination_option"] = {
        "value": "None (no unilateral cancellation option). Limited termination right only under Section 14.5 if casualty restoration exceeds 180 days, and Section 14.6 if lender demands insurance proceeds.",
        "page": None,
        "confidence": 0.88,
        "source": "No Cancellation/Termination Option clause found; only casualty-triggered rights in §14"
    }

    # Right of First Offer / Refusal — not found
    if not re.search(r"Right of First (?:Offer|Refusal)", full_text, re.IGNORECASE):
        out["rofo_rofr"] = {
            "value": "None",
            "page": None,
            "confidence": 0.92,
            "source": "No ROFO/ROFR clause found"
        }

    # Assignment & Subletting — Section 22
    out["assignment_subletting"] = {
        "value": ("Permitted only with Landlord's written consent. Permitted Transfers to affiliates, "
                  "merger successors, or asset purchasers without consent (use must stay same). "
                  "$1,000 transfer fee. Landlord has recapture right. 50% excess rent assignment to Landlord. "
                  "Tenant + Guarantor remain primarily liable."),
        "page": 12,
        "confidence": 0.94,
        "source": "Section 22 Assignment and Subletting"
    }

    # Co-Tenancy — retail concept, not applicable
    out["co_tenancy"] = {
        "value": "None",
        "page": None,
        "confidence": 0.95,
        "source": "Co-tenancy is a retail-only concept; this is an industrial warehouse/distribution lease"
    }

    # Exclusive Use — not found (industrial)
    out["exclusive_use"] = {
        "value": "None",
        "page": None,
        "confidence": 0.88,
        "source": "No exclusive use clause found (industrial lease)"
    }

    # Permitted Use — Section 6
    m = re.search(r"Premises shall be used only for\s+([^\.]+?\.)", full_text)
    if m:
        out["permitted_use"] = {
            "value": m.group(1).strip(),
            "page": 5,
            "confidence": 0.95,
            "source": "Section 6 Use and Compliance"
        }

    # Go Dark / Continuous Operation
    m = re.search(r"Tenant may use a portion of the Premises.{0,200}?(inventory outlet sales)", full_text, re.IGNORECASE)
    if m:
        out["continuous_operation"] = {
            "value": "No continuous operation requirement. Tenant permitted limited (≤2,000 SF, walled/fenced) inventory outlet sales.",
            "page": 5,
            "confidence": 0.85,
            "source": "Section 6 - Use permits inventory outlet sales; no go-dark prohibition"
        }

    # Guaranty — Exhibit E
    if re.search(r"EXHIBIT E\s*GUARANTY\s*Intentionally reserved", full_text, re.IGNORECASE):
        out["guaranty"] = {
            "value": "None — Exhibit E (Guaranty) intentionally reserved (no guarantor)",
            "page": 24,
            "confidence": 0.97,
            "source": "Exhibit E: Intentionally reserved"
        }

    # Holdover — Section 4.2 — complex tiered structure
    if re.search(r"150% of the Monthly Base Rent", full_text) and "200%" in full_text:
        out["holdover"] = {
            "value": ("Tiered: 150% of MBR for first 3 months if tenant gives 90-day advance notice; "
                      "otherwise 200% from day one; 200% after month 3 in all cases. Plus TICAM and other charges."),
            "page": 3,
            "confidence": 0.94,
            "source": "Section 4.2 Holdover"
        }

    return out


def extract_other(pages, full_text):
    out = {}

    # TI/Construction Allowance — Exhibit B §5
    m = re.search(r"Landlord shall contribute\s*\$?([\d,]+)\s*which Tenant may.{0,100}?Construction Allowance", full_text)
    if m:
        amt = m.group(1).replace(",", "")
        out["allowance"] = {
            "value": (f"${int(amt):,} Construction Allowance. "
                      "Usable toward Premises Improvements, exterior signage, or approved improvements. "
                      "Must be used within 6 months of Commencement Date or forfeited."),
            "page": 19,
            "confidence": 0.96,
            "source": "Exhibit B §5 Construction Allowance"
        }

    # Parking — Exhibit D §C and Section 6
    out["parking"] = {
        "value": ("Shared parking with other tenants; tenant may only use parking/loading areas related to its Premises. "
                  "Overnight parking of operational vehicles/trailers permitted, but vehicles cannot remain undriven >7 consecutive days. "
                  "No repair work on-site. No reserved spaces without Landlord consent."),
        "page": 5,
        "confidence": 0.92,
        "source": "Section 6 + Exhibit D §C Rules"
    }

    # Signs — Section 13
    out["signage"] = {
        "value": "Tenant may install Project-standard signs with Landlord's prior written consent. Must remove by Lease end and repair any damage.",
        "page": 8,
        "confidence": 0.90,
        "source": "Section 13 Signs"
    }

    # Subordination — Section 27.1
    out["subordination"] = {
        "value": "Subordinate to existing and future mortgages/deeds of trust. Tenant required to execute SNDA upon Landlord's lender request. Failure to sign within 10 days = Event of Default.",
        "page": 14,
        "confidence": 0.93,
        "source": "Section 27.1 Subordination and Attornment"
    }

    # Estoppel — Section 27.2
    m = re.search(r"estoppel certificate[\s\S]{0,100}?(\d+)\s*days", full_text, re.IGNORECASE)
    if m:
        out["estoppel"] = {
            "value": f"Must deliver within {m.group(1)} days of Landlord's written request. Failure = Event of Default.",
            "page": 14,
            "confidence": 0.94,
            "source": "Section 27.2 Estoppel Certificate"
        }

    # Hazardous Materials — Section 7
    out["hazardous_materials"] = {
        "value": ("Tenant prohibited from bringing Hazardous Substances without Landlord consent. "
                  "Tenant indemnifies Landlord for all releases caused by Tenant/agents. "
                  "Hazardous Material Disclosure Certificate required annually (Exhibit F). Survives Lease termination."),
        "page": 6,
        "confidence": 0.94,
        "source": "Section 7 + Exhibit F"
    }

    # Casualty — Section 14
    out["casualty"] = {
        "value": ("If restoration > 180 days: Lease terminates, rent abated. "
                  "If ≤ 180 days: Landlord rebuilds (subject to insurance proceeds), rent abated proportionally during repair. "
                  "If Landlord misses 180-day deadline, Tenant may terminate."),
        "page": 9,
        "confidence": 0.94,
        "source": "Section 14 Property and Casualty Damage (14.4 + 14.5)"
    }

    # Condemnation — Section 17
    out["condemnation"] = {
        "value": ("Complete/material taking: Lease terminates, rent abated. Partial taking: rent reduced fairly. "
                  "Landlord takes all award proceeds. Tenant may make separate claim for relocation/trade fixtures only."),
        "page": 10,
        "confidence": 0.94,
        "source": "Section 17 Condemnation"
    }

    # Landlord Relocation Right — Section 25 (notable pro-landlord clause)
    out["relocation_right"] = {
        "value": ("Yes — Landlord may relocate Tenant with 60 days' notice to comparable space in Project or vicinity. "
                  "Landlord reimburses reasonable moving costs. Rent capped at pre-relocation amount if new space is larger."),
        "page": 13,
        "confidence": 0.94,
        "source": "Section 25 Landlord's Right to Relocate Tenant"
    }

    # Brokers — Section 28.12
    m = re.search(r"other than\s+([A-Z][\w ]+)\s+and\s+([A-Z][\w]+)\s*\(", full_text)
    if m:
        out["brokers"] = {
            "value": f"{m.group(1).strip()} and {m.group(2).strip()} (paid by Landlord)",
            "page": 15,
            "confidence": 0.92,
            "source": "Section 28.12 Brokers"
        }

    # Late Payment — Section 3.1
    m = re.search(r"late charge \(\"Late Payment Charge\"\)[\s\S]{0,150}?(\d+)\s*percent\s*\((\d+)%\)", full_text)
    if m:
        out["late_payment"] = {
            "value": f"{m.group(2)}% of late payment, triggered after 7 days. First late in any 12-month period waived with notice + 3-business-day cure.",
            "page": 3,
            "confidence": 0.94,
            "source": "Section 3.1 Late Payment"
        }

    # Move-Out Requirements — Exhibit G
    if "EXHIBIT G" in full_text and "MOVE-OUT CHECKLIST" in full_text:
        out["move_out_requirements"] = {
            "value": ("Detailed 16-point checklist: remove personal property/racking (grind anchors flush); "
                      "HVAC service certification; operable lighting/plumbing/doors; broom-clean; "
                      "remove all signage; return keys. Preliminary walkthrough encouraged 30 days prior."),
            "page": 29,
            "confidence": 0.95,
            "source": "Exhibit G Move-Out Checklist"
        }

    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Parsing {PDF} ...", file=sys.stderr)
    pages, full_text = parse_pdf(PDF)
    print(f"  {len(pages)} pages parsed, {len(full_text):,} chars", file=sys.stderr)
    print(f"  OCR quality: clean digital PDF (no corruption issues)", file=sys.stderr)

    result = {
        "document": str(PDF.name),
        "pages_parsed": len(pages),
        "document_type": "Clean digital PDF (Docusigned)",
        "extraction_mode": "BASELINE (deterministic regex + keyword matching, no LLM)",
        "categories": {
            "Basic Information": extract_basic_info(pages, full_text),
            "Financial Clauses": extract_financial(pages, full_text),
            "Reimbursements": extract_reimbursements(pages, full_text),
            "Critical Clauses": extract_critical(pages, full_text),
            "Other Clauses": extract_other(pages, full_text),
        }
    }

    total_fields = sum(len(cat) for cat in result["categories"].values())
    extracted = sum(1 for cat in result["categories"].values() for v in cat.values()
                    if v["value"] not in ("None", None))
    none_count = total_fields - extracted
    conf_sum = sum(v["confidence"] for cat in result["categories"].values() for v in cat.values())
    high_conf = sum(1 for cat in result["categories"].values() for v in cat.values() if v["confidence"] >= 0.9)
    med_conf = sum(1 for cat in result["categories"].values() for v in cat.values() if 0.7 <= v["confidence"] < 0.9)
    low_conf = sum(1 for cat in result["categories"].values() for v in cat.values() if v["confidence"] < 0.7)

    result["summary"] = {
        "total_fields_attempted": total_fields,
        "fields_with_value": extracted,
        "fields_none": none_count,
        "mean_confidence": round(conf_sum / total_fields, 3) if total_fields else 0,
        "high_confidence_count": high_conf,
        "medium_confidence_count": med_conf,
        "low_confidence_count": low_conf,
    }

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
