"""Agentic lease extraction.

This package replaces the single-shot LLM extractor with a ReAct-style
agent loop that plans, uses tools to interact with the lease document,
reflects on intermediate results, and finalizes a grounded answer.

Modules:
    ollama_client  — thin wrapper around Ollama's /api/chat with tool-call
                     parsing + JSON-mode fallback for non-tool-capable models.
    tools          — the toolbelt the agent can call: search_document,
                     read_page, read_clause, check_definitions,
                     check_amendment_override, cross_reference,
                     finalize_answer.
    field_agent    — ReAct loop for a single field. Includes self-consistency
                     (vote of N samples for Number fields) and retry-on-
                     low-confidence with widened context.
    critique_agent — reviews a finalized answer and can request re-extraction.
    orchestrator   — coordinates extraction across 72 fields for a tenant,
                     handles dependencies, invokes critique, runs LeaseLens.
"""
