"""Deterministic stand-in for `OllamaAgentClient`.

Used when `EXTRACTOR_BACKEND=stub` so the extraction pipeline can run
end-to-end without contacting Ollama. Every per-question call returns a
fixed `{"answer": "NO", "value": null, "reasoning": "stub"}` response,
which lets the playbook executor walk every decision tree to its `NO`
branch and terminate cleanly.

Use cases:
  - End-to-end tests that exercise the API + Celery + DB pipeline
    without an LLM in the loop.
  - Local smoke tests on machines where Ollama is not installed.
  - Offline CI.

Drop-in replacement for `OllamaAgentClient` — same `.chat()` /
`.chat_json()` shape returning `AgentStep` / `dict`.
"""
from __future__ import annotations

from typing import Any

from app.agents.ollama_client import AgentStep


class StubAgentClient:
    """No-op LLM client. Returns deterministic placeholder answers."""

    # Same constructor signature as OllamaAgentClient so callers can swap.
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        num_ctx: int | None = None,
        timeout: int | None = None,
    ):
        self.model = model or "stub"
        self.temperature = temperature if temperature is not None else 0.0
        self.num_ctx = num_ctx or 0

    # ------------------------------------------------------------------
    # Tool-calling mode -- returns no tool calls + an empty assistant
    # message. The Coordinator's tool-call loop terminates immediately.
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AgentStep:
        return AgentStep(content="", tool_calls=[])

    # ------------------------------------------------------------------
    # JSON mode -- the per-question executor calls this. Returning
    # `answer="NO"` makes every playbook walk its NO branch which
    # terminates with RECORD_NONE / RECORD_LITERAL / FINALIZE depending
    # on the field's tree shape.
    # ------------------------------------------------------------------
    def chat_json(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
    ) -> dict[str, Any]:
        return {"answer": "NO", "value": None, "reasoning": "stub"}
