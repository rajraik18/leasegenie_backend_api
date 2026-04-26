"""Specialist agents.

Each specialist owns one BRD category and:
    * selects which playbooks apply to a given (abstract_type, property_type)
    * executes them via the PlaybookExecutor
    * applies category-specific cross-field rules before returning

The coordinator dispatches to specialists in a dependency-aware order so that
facts from earlier specialists (e.g. Basic Info's LCD) are available as
shared_facts to later ones (e.g. Other Clauses' Allowance).
"""
from app.agents.specialists.base import (
    SpecialistAgent, SpecialistResult, FieldOutcome,
)
from app.agents.specialists.basic_info import BasicInfoAgent
from app.agents.specialists.financial import FinancialAgent
from app.agents.specialists.reimbursement import ReimbursementAgent
from app.agents.specialists.critical import CriticalAgent
from app.agents.specialists.other import OtherClausesAgent


SPECIALISTS = {
    "Basic Information": BasicInfoAgent,
    "Financial Clauses": FinancialAgent,
    "Reimbursements": ReimbursementAgent,
    "Critical Clauses": CriticalAgent,
    "Other Lease Clauses": OtherClausesAgent,
}

__all__ = [
    "SPECIALISTS",
    "SpecialistAgent",
    "SpecialistResult",
    "FieldOutcome",
    "BasicInfoAgent",
    "FinancialAgent",
    "ReimbursementAgent",
    "CriticalAgent",
    "OtherClausesAgent",
]
