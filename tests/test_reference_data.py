"""Test that the BRD spreadsheet loads correctly."""
from app.core.reference_data import load_reference_data
from app.config import settings


def test_brd_loads_72_fields():
    ref = load_reference_data(settings.brd_path)
    # BRD defines 72 fields
    assert len(ref.fields) >= 70, f"expected ~72 fields, got {len(ref.fields)}"


def test_annual_base_rent_is_number():
    ref = load_reference_data(settings.brd_path)
    f = ref.get("annual_base_rent")
    assert f is not None
    assert f.output_type == "Number"
    assert f.category == "Financial Clauses"
    assert len(f.keywords) > 0
    assert len(f.questions) > 0


def test_applicability_scoping():
    ref = load_reference_data(settings.brd_path)

    full_retail = ref.list_fields("Full Abstract", "Retail")
    basic_office = ref.list_fields("Basic Economic Abstract", "Office")

    # Full Abstract should include far more fields than Basic Economic
    assert len(full_retail) > len(basic_office)
    # Basic Economic should have ~16 office fields
    assert 10 < len(basic_office) < 30


def test_all_categories_present():
    ref = load_reference_data(settings.brd_path)
    categories = {f.category for f in ref.fields.values()}
    expected = {
        "Basic Information",
        "Financial Clauses",
        "Reimbursements",
        "Critical Clauses",
        "Other Lease Clauses",
    }
    assert expected.issubset(categories), f"missing: {expected - categories}"
