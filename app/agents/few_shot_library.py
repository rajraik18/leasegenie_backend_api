"""Expanded few-shot library — adds canonical Q/A examples for every playbook
that didn't already have them in FEW_SHOT_LIBRARY.

Each example is grounded in actual phrasing from the 17-doc corpus (Sample 1
ProLogis / Pine Timbers / Advance Stores, Sample 6 HMBP-BCP / Beacon Commerce
Park / Garner Appliance, and a few synthetic examples for retail fields).

The library is loaded by `app.agents.playbook_executor._build_few_shot_block`.
"""

# Keyed by field_id. Each value is a list of example dicts:
#   {qid?, clause, expected: {answer, value, is_monthly?}, note?}
EXTENDED_FEW_SHOT_LIBRARY = {

    # ========================================================================
    # BASIC INFORMATION
    # ========================================================================
    "tenant_name": [
        {
            "qid": "Q1",
            "clause": "Tenant: Advance Stores Company, Incorporated, a Virginia corporation",
            "expected": {"answer": "YES", "value": "Advance Stores Company, Incorporated"},
            "note": "Full legal name including entity suffix; drop state of incorporation.",
        },
        {
            "qid": "Q1",
            "clause": "TENANT: GARNER T.V. & APPLIANCES INC., a North Carolina corporation dba GARNER APPLIANCE & MATTRESS",
            "expected": {"answer": "YES", "value": "GARNER T.V. & APPLIANCES INC. (dba GARNER APPLIANCE & MATTRESS)"},
            "note": "Include DBA parenthetically when present.",
        },
    ],
    "landlord_name": [
        {
            "qid": "Q1",
            "clause": "THIS LEASE AGREEMENT is made this 15 day of May, 2009, between ProLogis (\"Landlord\"), and the Tenant named below.",
            "expected": {"answer": "YES", "value": "ProLogis"},
            "note": "Take the name that's explicitly defined as \"Landlord\".",
        },
        {
            "qid": "Q1",
            "clause": "LANDLORD: HMBP - BCP LLC, a North Carolina limited liability company",
            "expected": {"answer": "YES", "value": "HMBP - BCP LLC"},
        },
    ],
    "lease_date": [
        {
            "qid": "Q1",
            "clause": "THIS LEASE AGREEMENT is made this 15 day of May, 2009",
            "expected": {"answer": "YES", "value": "May 15, 2009"},
            "note": "Reconstruct the full date even when written as \"this __ day of __\".",
        },
        {
            "qid": "Q1",
            "clause": "LEASE DATE: 8/2/2024",
            "expected": {"answer": "YES", "value": "August 2, 2024"},
            "note": "Expand numeric dates to month name form for consistency.",
        },
    ],
    "lease_expiration_date": [
        {
            "qid": "Q1",
            "clause": "Lease Term: Beginning on the Commencement Date and ending on the last day of the 62nd full calendar month thereafter.\nCommencement Date: June 1, 2009",
            "expected": {"answer": "YES", "value": "July 31, 2014"},
            "note": "Derived: June 1, 2009 + 62 full calendar months = July 31, 2014.",
        },
        {
            "qid": "Q1",
            "clause": "08/01/2029-09/30/2029 $16.49 $37,502.10",
            "expected": {"answer": "YES", "value": "September 30, 2029"},
            "note": "End of the final rent-step period = lease expiration.",
        },
    ],
    "lease_term_yrs": [
        {
            "qid": "Q1",
            "clause": "A period of sixty-two (62) months, beginning on the Commencement Date",
            "expected": {"answer": "YES", "value": "5.17"},
            "note": "62 months / 12 = 5.17 years (derived).",
        },
    ],
    "original_lease_commencement_date": [
        {
            "qid": "Q1",
            "clause": "Commencement Date: June 1, 2009",
            "expected": {"answer": "YES", "value": "June 1, 2009"},
        },
        {
            "qid": "Q1",
            "clause": "1.4. Commencement Date. The commencement date of this Lease shall be August 1, 2024.",
            "expected": {"answer": "YES", "value": "August 1, 2024"},
        },
    ],
    "term_commencement_date": [
        {
            "qid": "Q1",
            "clause": "The Term shall commence on June 1, 2009 and expire on July 31, 2014.",
            "expected": {"answer": "YES", "value": "June 1, 2009"},
        },
    ],
    "rent_commencement_date": [
        {
            "qid": "Q1",
            "clause": "Operating Expenses for the period from June 1, 2009 through July 31, 2009 will be $0.00. Commencing August 1, 2009, during each month of the Lease Term",
            "expected": {"answer": "YES", "value": "August 1, 2009"},
            "note": "Rent commencement may differ from term commencement when there's a free-rent period.",
        },
    ],
    "most_recent_lease_start": [
        {
            "qid": "Q1",
            "clause": "(No amendments found; base lease commencement applies.)",
            "expected": {"answer": "NO", "value": "None"},
            "note": "Only populated when amendments reset the effective start; otherwise None.",
        },
    ],
    "leased_rsf": [
        {
            "qid": "Q1",
            "clause": "That portion of the Building, containing approximately 38,620 rentable square feet",
            "expected": {"answer": "YES", "value": "38,620"},
        },
        {
            "qid": "Q1",
            "clause": "PREMISES: Shall be deemed to be 27,298 square feet (\"SF\") located at 4900 Jones Sausage Road, Suite 125",
            "expected": {"answer": "YES", "value": "27,298"},
        },
    ],
    "building": [
        {
            "qid": "Q1",
            "clause": "Building: Pine Timbers Distribution Center #2\n4660 Pine Timbers\nSuite 160\nHouston, TX 77041",
            "expected": {"answer": "YES", "value": "Pine Timbers Distribution Center #2"},
            "note": "Extract the building name only; street address goes in street_address.",
        },
    ],
    "suite": [
        {
            "qid": "Q1",
            "clause": "4660 Pine Timbers, Suite 160, Houston, TX 77041",
            "expected": {"answer": "YES", "value": "160"},
            "note": "Numeric suite only; drop the 'Suite' prefix.",
        },
        {
            "qid": "Q1",
            "clause": "4900 Jones Sausage Road, Suite 125, Garner, North Carolina 27529",
            "expected": {"answer": "YES", "value": "125"},
        },
    ],
    "street_address": [
        {
            "qid": "Q1",
            "clause": "4660 Pine Timbers\nSuite 160\nHouston, TX 77041",
            "expected": {"answer": "YES", "value": "4660 Pine Timbers, Suite 160"},
        },
        {
            "qid": "Q1",
            "clause": "4900 Jones Sausage Road, Suite 125, Garner, North Carolina 27529",
            "expected": {"answer": "YES", "value": "4900 Jones Sausage Road, Suite 125"},
        },
    ],
    "city": [
        {
            "qid": "Q1",
            "clause": "Houston, TX 77041",
            "expected": {"answer": "YES", "value": "Houston"},
        },
        {
            "qid": "Q1",
            "clause": "Garner, North Carolina 27529",
            "expected": {"answer": "YES", "value": "Garner"},
        },
    ],
    "state": [
        {
            "qid": "Q1",
            "clause": "Houston, TX 77041",
            "expected": {"answer": "YES", "value": "TX"},
            "note": "Two-letter postal abbreviation preferred.",
        },
        {
            "qid": "Q1",
            "clause": "Garner, North Carolina 27529",
            "expected": {"answer": "YES", "value": "NC"},
        },
    ],
    "property_name": [
        {
            "qid": "Q1",
            "clause": "Project: Pine Timbers Distribution Center #2",
            "expected": {"answer": "YES", "value": "Pine Timbers Distribution Center #2"},
        },
        {
            "qid": "Q1",
            "clause": "That certain project known as Beacon Commerce Park (\"Project\")",
            "expected": {"answer": "YES", "value": "Beacon Commerce Park"},
        },
    ],
    "lease_guarantor": [
        {
            "qid": "Q1",
            "clause": "EXHIBIT E GUARANTY Intentionally reserved.",
            "expected": {"answer": "NO", "value": "None"},
            "note": "\"Intentionally reserved\" = no guarantor.",
        },
    ],

    # ========================================================================
    # FINANCIAL CLAUSES
    # ========================================================================
    "future_rent_steps": [
        {
            "qid": "Q1",
            "clause": (
                "June 1, 2009 through July 31, 2009  $0.00\n"
                "August 1, 2009 through July 31, 2012  $8,882.60\n"
                "August 1, 2012 through July 31, 2014  $9,655.00"
            ),
            "expected": {
                "answer": "YES",
                "value": "6/1/2009–7/31/2009: $0 (free rent); 8/1/2009–7/31/2012: $8,882.60/mo; 8/1/2012–7/31/2014: $9,655.00/mo",
            },
            "note": "Include each step with dates and amount; note free-rent periods.",
        },
        {
            "qid": "Q1",
            "clause": (
                "08/01/2024-07/31/2025  $13.55  $30,823.99\n"
                "08/01/2025-07/31/2026  $14.09  $32,056.95\n"
                "08/01/2026-07/31/2027  $14.66  $33,339.23"
            ),
            "expected": {
                "answer": "YES",
                "value": "Annual 4% escalator: Yr1 $30,823.99/mo ($13.55/SF); Yr2 $32,056.95/mo ($14.09/SF); Yr3 $33,339.23/mo ($14.66/SF)",
            },
            "note": "Format columns as month amount plus per-SF rate.",
        },
    ],
    "annual_base_rent": [
        # already seeded
    ],
    "security_deposit": [
        # already seeded
    ],
    "pro_rata": [
        # already seeded
    ],
    "percentage_rent": [
        {
            "qid": "Q1",
            "clause": "Tenant shall pay as Percentage Rent six percent (6%) of Gross Sales in excess of the Breakpoint.",
            "expected": {"answer": "YES", "value": "6% of Gross Sales above Breakpoint"},
            "note": "Retail only. Include rate + qualifier.",
        },
    ],
    "breakpoint": [
        {
            "qid": "Q1",
            "clause": "\"Breakpoint\" means Annual Base Rent divided by the Percentage Rent rate.",
            "expected": {"answer": "YES", "value": "Natural breakpoint (Base Rent ÷ Percentage Rate)"},
            "note": "Retail. Natural breakpoint = Base Rent / Pct. Artificial = stated dollar figure.",
        },
    ],
    "late_payment": [
        # already seeded
    ],

    # ========================================================================
    # REIMBURSEMENTS
    # ========================================================================
    "cam": [
        {
            "qid": "Q1",
            "clause": "Tenant shall pay Landlord's Proportionate Share of CAM Charges. CAM Charges shall include water/sewer, HVAC, mowing, landscape, trash removal, parking lot, common area lighting, roof membrane, property management.",
            "expected": {"answer": "YES", "value": "Tenant pays Proportionate Share of CAM; inclusions defined in §5.4"},
            "note": "Summarize — don't copy the whole list.",
        },
    ],
    "cam_inclusion": [
        {
            "qid": "Q1",
            "clause": "CAM Charges shall include common water and sewer services, HVAC equipment, filter and service contracts, mowing, landscape maintenance, rubbish removal, sidewalk, parking lot, truck court, common area lighting, maintenance of common signs, exterior painting, downspouts and gutters, security services, non-structural portions of the roof, property management services, snow and ice removal, fire alarm monitoring, storm water fees.",
            "expected": {"answer": "YES", "value": "Water/sewer, HVAC, landscaping, trash, parking, lighting, exterior maintenance, roof membrane, security, property management, snow/ice, fire alarm, storm water"},
            "note": "Condense the inclusion list into key categories.",
        },
    ],
    "cam_exclusion": [
        {
            "qid": "Q1",
            "clause": "CAM Charges shall NOT include: (A) wages above building-manager grade; (B) leasehold improvements for specific tenants; (D) brokerage commissions; (E) financing costs; (F) tort settlements; (G) ground rent; (K) insured casualty repairs; (M) depreciation.",
            "expected": {"answer": "YES", "value": "Excludes: wages above manager-level, tenant-specific improvements, brokerage, financing, tort settlements, ground rent, insured repairs, depreciation"},
        },
    ],
    "caps_on_cam": [
        {
            "qid": "Q1",
            "clause": "Tenant shall not be obligated to pay for Controllable Operating Expenses in any year to the extent they have increased by more than six percent (6.000%) per annum, compounded annually on a cumulative basis.",
            "expected": {"answer": "YES", "value": "6% per annum, cumulative (Controllable OpEx only — excludes Taxes, insurance, utilities)"},
            "note": "Always note whether the cap is cumulative/non-cumulative and what's excluded.",
        },
    ],
    "re_taxes": [
        {
            "qid": "Q1",
            "clause": "Landlord shall pay all taxes, assessments and governmental charges (\"Taxes\") that accrue against the Project during the Lease Term, which shall be included as part of the Operating Expenses charged to Tenant.",
            "expected": {"answer": "YES", "value": "Tenant pays Proportionate Share of all Taxes via OpEx/TICAM"},
            "note": "Flag when Taxes are part of OpEx vs. a direct pass-through.",
        },
    ],
    "landlord_insurance": [
        {
            "qid": "Q1",
            "clause": "Landlord shall maintain (i) all risk property insurance covering the full replacement cost of the Building, and (ii) commercial general liability insurance, with a minimum limit of $1,000,000 per occurrence and a minimum umbrella limit of $1,000,000",
            "expected": {"answer": "YES", "value": "All-risk property (full replacement cost) + CGL $1M/occ + $1M umbrella; premiums included in CAM"},
        },
    ],
    "tenant_insurance_requirements": [
        {
            "qid": "Q1",
            "clause": "Tenant shall procure and maintain commercial general liability insurance of at least $2,000,000.00 per occurrence with a $2,000,000 aggregate; all-risk property on Tenant's property; Workers Compensation statutory; Business Automobile $1,000,000 combined single limit.",
            "expected": {"answer": "YES", "value": "CGL $2M/occ $2M agg; all-risk on Tenant property at full replacement cost; Workers Comp statutory; Business Auto $1M CSL"},
        },
    ],
    "utilities": [
        {
            "qid": "Q1",
            "clause": "Tenant shall pay for all water, gas, electricity, heat, light, power, telephone, sewer, sprinkler services, refuse and trash collection, and other utilities and services used on the Premises.",
            "expected": {"answer": "YES", "value": "Tenant pays all utilities directly (water, gas, electric, sewer, telecom, trash)"},
        },
    ],
    "base_year": [
        {
            "qid": "Q1",
            "clause": "(No base year provision — tenant pays Proportionate Share of actual TICAM from Commencement Date.)",
            "expected": {"answer": "NO", "value": "None"},
            "note": "Net leases usually have no base year stop.",
        },
    ],
    "base_year_amount": [
        {
            "qid": "Q1",
            "clause": "Base Year Expenses shall equal the actual Operating Expenses for calendar year 2024, as reasonably determined by Landlord.",
            "expected": {"answer": "YES", "value": "2024 actual OpEx (amount TBD by Landlord)"},
            "note": "Base year amount may be the actual; note when it's stated in dollars.",
        },
    ],
    "mgmt_fee": [
        {
            "qid": "Q1",
            "clause": "Property management fees payable to a property manager, not to exceed three percent (3%) of the gross rental payments payable hereunder by Tenant for Base Rent and Operating Expenses",
            "expected": {"answer": "YES", "value": "3% of Base Rent + Operating Expenses (cap); 10% of OpEx if no manager"},
            "note": "Record both the third-party cap and the no-manager fallback.",
        },
    ],
    "admin_fee": [
        {
            "qid": "Q1",
            "clause": "an administration fee of ten percent (10%) of Operating Expenses payable to Landlord",
            "expected": {"answer": "YES", "value": "10% of Operating Expenses (no-manager fallback)"},
        },
    ],
    "gross_up": [
        {
            "qid": "Q1",
            "clause": "Operating Expenses shall be grossed up to reflect 95% occupancy if the Building is less than 95% occupied.",
            "expected": {"answer": "YES", "value": "Gross-up to 95% occupancy"},
        },
    ],

    # ========================================================================
    # CRITICAL LEASE CLAUSES
    # ========================================================================
    "renewal_options": [
        # already seeded
    ],
    "holdover": [
        # already seeded
    ],
    "tenant_termination": [
        {
            "qid": "Q1",
            "clause": "Tenant shall have the right at any time on or before December 31, 2011 to send Landlord written notice that Tenant has elected to terminate this Lease effective on June 30, 2012... conditioned upon Tenant paying $76,282.14 to Landlord on, or before, June 30, 2012.",
            "expected": {"answer": "YES", "value": "Cancellation option exercisable by 12/31/2011, effective 6/30/2012, termination fee $76,282.14"},
            "note": "Include window, effective date, and termination fee.",
        },
    ],
    "landlord_termination": [
        {
            "qid": "Q1",
            "clause": "If Landlord elects option (a), Tenant's assignment request triggers Landlord's right to terminate this Lease as to the affected space.",
            "expected": {"answer": "YES", "value": "Landlord recapture on tenant assignment request (space-specific termination)"},
        },
    ],
    "rofo": [
        {
            "qid": "Q1",
            "clause": "Offered Space 1 shall mean the 17,640 square feet space located at 4660 Pine Timbers, Suite 150. If at any time during the Lease Term any lease for any portion of the Offered Space shall expire, then Landlord, before offering such space to anyone, shall offer to Tenant the right to include the Offered Space within the Premises",
            "expected": {"answer": "YES", "value": "ROFO on 17,640 SF at Suite 150; 10-day acceptance; FMV rent subject to arbitration"},
        },
    ],
    "rofr": [
        {
            "qid": "Q1",
            "clause": "If Landlord receives a bona fide offer from a third party for the Offered Space, Landlord shall first offer Tenant the right to lease on the same terms.",
            "expected": {"answer": "YES", "value": "ROFR matching third-party offer"},
        },
    ],
    "right_of_expansion": [
        {
            "qid": "Q1",
            "clause": "Tenant shall have a one-time right to expand into the adjacent 10,000 SF space, exercisable by December 31, 2026.",
            "expected": {"answer": "YES", "value": "10,000 SF expansion option, exercisable by 12/31/2026"},
        },
    ],
    "contraction_option": [
        {
            "qid": "Q1",
            "clause": "Tenant may give back up to 5,000 square feet of the Premises by delivering written notice and paying the unamortized TI allowance.",
            "expected": {"answer": "YES", "value": "Tenant may contract up to 5,000 SF upon notice + unamortized TI repayment"},
        },
    ],
    "purchase_option": [
        {
            "qid": "Q1",
            "clause": "Tenant shall have no option to purchase the Premises.",
            "expected": {"answer": "NO", "value": "None"},
        },
    ],
    "co_tenancy": [
        {
            "qid": "Q1",
            "clause": "If either Anchor Tenant ceases operations, Tenant shall have the right to pay Substitute Rent equal to 3% of Gross Sales in lieu of Base Rent.",
            "expected": {"answer": "YES", "value": "Anchor failure triggers substitute rent (3% of Gross Sales)"},
            "note": "Retail only.",
        },
    ],
    "sales_kick_out": [
        {
            "qid": "Q1",
            "clause": "If Tenant's Gross Sales in any Lease Year 4 or later fall below $2,500,000, Tenant may terminate on 60 days' notice plus the unamortized TI.",
            "expected": {"answer": "YES", "value": "Sales kick-out at $2.5M (Year 4+); 60-day notice; pay unamortized TI"},
            "note": "Retail only.",
        },
    ],
    "exclusive_use": [
        {
            "qid": "Q1",
            "clause": "Tenant has the exclusive right to sell major appliances (refrigerators, washers, dryers, dishwashers) at the Project.",
            "expected": {"answer": "YES", "value": "Exclusive: major appliances at Project"},
            "note": "Capture category list precisely.",
        },
    ],
    "permitted_use": [
        {
            "qid": "Q1",
            "clause": "The Premises shall be used only for the purpose of receiving, storing, shipping and selling (but limited to wholesale sales) products, materials and merchandise",
            "expected": {"answer": "YES", "value": "Receiving, storing, shipping, wholesale selling of products/materials/merchandise; light mfg w/ consent"},
        },
        {
            "qid": "Q1",
            "clause": "The Premises shall be used only for general office, warehouse and distribution uses... Tenant may use a portion (no larger than 2,000 SF) for inventory outlet sales",
            "expected": {"answer": "YES", "value": "Office, warehouse, distribution; up to 2,000 SF inventory outlet sales"},
        },
    ],
    "continuous_operation": [
        {
            "qid": "Q1",
            "clause": "Tenant shall operate its business during all hours required by Landlord.",
            "expected": {"answer": "YES", "value": "Continuous operation required during Landlord-specified hours"},
            "note": "Retail only.",
        },
    ],
    "go_dark": [
        {
            "qid": "Q1",
            "clause": "Tenant may go dark after year 3 provided Tenant continues paying all Rent.",
            "expected": {"answer": "YES", "value": "Go-dark permitted after Year 3 (rent continues)"},
            "note": "Retail only.",
        },
    ],
    "monetary_default": [
        {
            "qid": "Q1",
            "clause": "Tenant shall fail to pay any installment of Base Rent when due, and such failure shall continue for a period of ten (10) days after Tenant's receipt of written notice from Landlord",
            "expected": {"answer": "YES", "value": "10 days after written notice from Landlord"},
        },
        {
            "qid": "Q1",
            "clause": "Tenant shall fail to pay any installment of Monthly Base Rent or any Additional Rent when due, and such failure shall continue for a period of seven (7) days",
            "expected": {"answer": "YES", "value": "7 days past due (after 1 free late-notice per 12-mo period)"},
        },
    ],
    "non_monetary_default": [
        {
            "qid": "Q1",
            "clause": "Tenant shall fail to comply with any provision of this Lease other than those specifically referred to in this Paragraph 23, and such default shall continue for more than thirty (30) days after Landlord shall have given Tenant written notice",
            "expected": {"answer": "YES", "value": "30 days after written notice; extended if cure needs longer with diligence"},
        },
    ],
    "landlord_restriction": [
        {
            "qid": "Q1",
            "clause": "Landlord shall not lease any other space in the Project to a direct competitor of Tenant engaged in appliance sales.",
            "expected": {"answer": "YES", "value": "Landlord may not lease to direct appliance-sales competitor"},
        },
    ],
    "landlord_s_recapture_rights": [
        {
            "qid": "Q1",
            "clause": "Upon receipt of Tenant's notice of a desire to assign or sublet, Landlord may terminate this Lease as to the space described within 30 days",
            "expected": {"answer": "YES", "value": "Landlord recapture within 30 days of assignment/sublet notice"},
        },
    ],

    # ========================================================================
    # OTHER LEASE CLAUSES
    # ========================================================================
    "allowance": [
        # already seeded
    ],
    "alteration": [
        {
            "qid": "Q1",
            "clause": "Interior, non-structural Tenant-Made Alterations, the cost of which exceeds $15,000 in each instance, shall be subject to Landlord's prior written consent.",
            "expected": {"answer": "YES", "value": "Landlord consent for interior non-structural alterations >$15,000; structural requires consent (sole discretion)"},
        },
        {
            "qid": "Q1",
            "clause": "Landlord's consent shall not be required for any Permitted Alteration... the costs of such Alteration is less than Fifty Thousand Dollars ($50,000.00) in the aggregate in any twelve (12) month period",
            "expected": {"answer": "YES", "value": "Consent required except Permitted Alterations <$50K/12mo, cosmetic, non-structural, no permit"},
        },
    ],
    "assignment_and_subletting": [
        {
            "qid": "Q1",
            "clause": "Tenant shall not assign this Lease or sublease the Premises without Landlord's prior written consent, which consent shall not be unreasonably withheld... Tenant may, without consent, assign to any entity into which Tenant is merged or consolidated, or to any entity to which substantially all of Tenant's assets are transferred (Permitted Transfer)",
            "expected": {"answer": "YES", "value": "Consent required (not unreasonably withheld); Permitted Transfers to affiliates/merger/asset sale w/o consent; 50% excess rent to Landlord"},
        },
    ],
    "sublease_provision": [
        {
            "qid": "Q1",
            "clause": "Tenant may sublet all or any portion of the Premises to affiliates without Landlord's consent; otherwise subleases require consent (not unreasonably withheld).",
            "expected": {"answer": "YES", "value": "Affiliate subleases without consent; others require reasonable consent"},
        },
    ],
    "parking": [
        {
            "qid": "Q1",
            "clause": "Tenant shall be entitled to park in common with other tenants in those areas designated for nonreserved parking. Landlord may allocate parking spaces if parking becomes crowded.",
            "expected": {"answer": "YES", "value": "Non-reserved shared parking; Landlord may allocate if crowded"},
        },
    ],
    "other_income_exterior_signage_storage": [
        {
            "qid": "Q1",
            "clause": "Landlord shall install Tenant's name on the Building directory monument. No other exterior signage without consent. See Addendum 8 (building sign specifications).",
            "expected": {"answer": "YES", "value": "Building directory monument + building-standard exterior sign with Landlord specs; interior door vinyl permitted"},
        },
    ],
    "advertisement": [
        {
            "qid": "Q1",
            "clause": "Tenant shall contribute $1,000 per annum to the Center's advertising fund, escalating with CPI.",
            "expected": {"answer": "YES", "value": "$1,000/yr advertising fund contribution, CPI-escalated"},
            "note": "Retail only.",
        },
    ],
    "marketing": [
        {
            "qid": "Q1",
            "clause": "Tenant shall participate in the Center's marketing fund at $0.25 per SF per year.",
            "expected": {"answer": "YES", "value": "$0.25/SF/year marketing fund"},
            "note": "Retail only.",
        },
    ],
    "reporting_of_gross_sales": [
        {
            "qid": "Q1",
            "clause": "Tenant shall deliver to Landlord a statement of Gross Sales within 60 days after the end of each Lease Year.",
            "expected": {"answer": "YES", "value": "Annual Gross Sales statement, within 60 days of Lease Year end"},
            "note": "Retail only.",
        },
    ],
    "reporting_of_financial_information": [
        {
            "qid": "Q1",
            "clause": "Upon written request by Landlord, but in no event more often than once per calendar year, Tenant shall provide annual financial statements including a balance sheet, income statement, statement of cash flows",
            "expected": {"answer": "YES", "value": "Annual financials on Landlord request (max 1×/yr): balance sheet, income statement, cash flows"},
        },
    ],
    "relocation": [
        {
            "qid": "Q1",
            "clause": "Landlord shall have the right, upon at least sixty (60) days prior written notice, to relocate Tenant to other space of similar position and comparable SF area. Landlord shall reimburse Tenant for reasonable moving costs.",
            "expected": {"answer": "YES", "value": "60-day notice relocation to comparable space; Landlord reimburses reasonable moving costs"},
        },
    ],
    "casualty": [
        {
            "qid": "Q1",
            "clause": "If restoration is estimated to exceed six (6) months, either Landlord or Tenant may elect to terminate this Lease within 30 days of Landlord's notice... Base Rent and Operating Expenses shall be abated for the period of repair and restoration",
            "expected": {"answer": "YES", "value": "Either party may terminate if restoration >6 months (ProLogis) / 180 days (HMBP); rent abated during repair proportionate to untenantable area"},
        },
    ],
    "condemnation": [
        {
            "qid": "Q1",
            "clause": "If any part of the Premises should be taken for any public use, and the Taking prevents or materially interferes with Tenant's use, this Lease shall terminate. Landlord shall be entitled to the entire award; Tenant may make a separate claim for moving expenses and Trade Fixtures.",
            "expected": {"answer": "YES", "value": "Lease terminates on material taking; Landlord gets award; Tenant may separately claim moving/Trade Fixtures"},
        },
    ],
    "subordination": [
        {
            "qid": "Q1",
            "clause": "This Lease shall be subject and subordinate at all times to the lien of any first mortgage now existing or hereafter created. Tenant agrees to attorn to any such holder.",
            "expected": {"answer": "YES", "value": "Subordinate to existing + future first mortgages; SNDA required (10-day delivery); failure = Event of Default"},
        },
    ],
    "hazardous_materials": [
        {
            "qid": "Q1",
            "clause": "Tenant shall not permit or cause any party to bring any Hazardous Material upon the Premises without Landlord's prior written consent. Tenant shall indemnify Landlord from any release caused by Tenant.",
            "expected": {"answer": "YES", "value": "No Hazardous Materials without consent (de minimis office/cleaning exempt); Tenant indemnity for Tenant-caused releases; Landlord indemnity for pre-existing"},
        },
    ],
    "repair_and_maintenance": [
        {
            "qid": "Q1",
            "clause": "Landlord shall maintain the structural soundness of the roof, foundation, and exterior walls. Tenant shall repair and maintain all other portions of the Premises (doors, HVAC, plumbing, windows, etc.).",
            "expected": {"answer": "YES", "value": "Landlord: roof structure, foundation, exterior walls (CAM-reimbursable if common). Tenant: all else incl. HVAC, plumbing, doors, windows, interior"},
        },
    ],

    # ========================================================================
    # NEW PLAYBOOKS (from Week 1 Step 2) — seed just the Q1
    # ========================================================================
    "move_out_conditions": [
        {
            "qid": "Q1",
            "clause": "Tenant is obligated to check and address prior to move-out: all lighting in good working order; truck doors and dock levelers serviced; HVAC inspection certification; sheet rock repaired; warehouse broom-clean; structural columns inspected; keys returned.",
            "expected": {"answer": "YES", "value": "Detailed move-out checklist (Addendum 9): HVAC cert, dock equip service, broom-clean warehouse, structural column inspection, key return, damage repair"},
        },
        {
            "qid": "Q1",
            "clause": "Tenant shall surrender the Premises in clean, good and tenantable condition, ordinary wear excepted. See Exhibit G Move-Out Checklist (16-point).",
            "expected": {"answer": "YES", "value": "Tenantable condition, ordinary wear excepted; Exhibit G 16-point checklist (personal property removal, HVAC cert, cleaning, signage removal, keys)"},
        },
    ],
    "notices": [
        {
            "qid": "Q2",
            "clause": "To Landlord at: ProLogis\n1201 West Loop North\nSuite 100\nHouston, TX 77055\n\nWith a copy to: ProLogis\n4545 Airport Way\nDenver, Colorado 80239",
            "expected": {"answer": "YES", "value": "ProLogis, 1201 West Loop North, Suite 100, Houston, TX 77055 (with copy to Denver)"},
        },
        {
            "qid": "Q3",
            "clause": "To Tenant at: Advance Stores Company, Incorporated\nP.O. Box 2710\nRoanoke, Virginia 24001\nAttn: Real Estate Department",
            "expected": {"answer": "YES", "value": "Advance Stores Company, Incorporated, P.O. Box 2710, Roanoke, VA 24001 (Attn: Real Estate Department)"},
        },
    ],
    "indemnification": [
        {
            "qid": "Q1",
            "clause": "Except for the negligence of Landlord, Tenant agrees to indemnify, defend and hold harmless Landlord from all losses, liabilities, damages, costs and expenses resulting from claims by third parties for injuries to any person... Except for the negligence of Tenant, Landlord agrees to indemnify, defend and hold harmless Tenant.",
            "expected": {"answer": "YES", "value": "Mutual indemnification; each party indemnifies except for the other's negligence; insurance is primary"},
        },
    ],
    "rules_and_regulations": [
        {
            "qid": "Q1",
            "clause": "Tenant shall comply with all reasonable rules and regulations established by Landlord. The current rules and regulations are attached hereto.",
            "expected": {"answer": "YES", "value": "R&R attached as Exhibit/Addendum; Landlord may modify if uniformly enforced; lease controls if conflict"},
        },
    ],
    "estoppel_certificate": [
        {
            "qid": "Q1",
            "clause": "Tenant agrees, within twenty (20) days after request of Landlord, to execute and deliver an estoppel certificate... Failure to deliver timely shall be an Event of Default.",
            "expected": {"answer": "YES", "value": "20-day delivery window; failure = Event of Default"},
        },
        {
            "qid": "Q2",
            "clause": "Within ten (10) days following written notice from Landlord, Tenant covenants and agrees that it shall execute and deliver to Landlord an estoppel certificate",
            "expected": {"answer": "YES", "value": "10 days"},
        },
    ],
    "force_majeure": [
        {
            "qid": "Q1",
            "clause": "Landlord shall not be held responsible for delays caused by strikes, lockouts, labor disputes, acts of God, governmental restrictions, enemy action, civil commotion, fire or casualty (\"Force Majeure\"). In no event shall Tenant's obligation to pay rent be delayed or excused by Force Majeure.",
            "expected": {"answer": "YES", "value": "Excuses non-monetary performance delays (strikes, Acts of God, governmental, civil commotion); rent obligations remain absolute"},
        },
    ],
    "brokers": [
        {
            "qid": "Q1",
            "clause": "Broker: Jarret Venghaus - Jones Lang LaSalle. Landlord shall pay a commission to the broker listed on the first page of this Lease based on the terms of a separate agreement.",
            "expected": {"answer": "YES", "value": "Jarret Venghaus (Jones Lang LaSalle); Landlord pays per separate agreement"},
        },
        {
            "qid": "Q1",
            "clause": "Brokers: Hartwell Realty and CBRE, which Brokers shall be compensated by Landlord pursuant to a separate agreement.",
            "expected": {"answer": "YES", "value": "Hartwell Realty + CBRE; Landlord pays per separate agreement"},
        },
    ],
}
