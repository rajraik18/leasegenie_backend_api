"""Thin wrapper around Ollama's chat API with tool-call support.

We use the official `ollama` Python client. Qwen2.5, Llama 3.1+, and Hermes3
all support OpenAI-style tool calls natively through Ollama. When a model
doesn't support tool calls, we fall back to a JSON-mode protocol where the
model emits `{"tool": "...", "arguments": {...}}` objects.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class AgentStep:
    """One turn of the agent loop."""
    content: str                 # assistant text (may be reasoning)
    tool_calls: list[ToolCall]   # tools the model wants to invoke


class OllamaAgentClient:
    """Maintains a chat history and exposes chat_with_tools for one turn."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        num_ctx: int | None = None,
        timeout: int | None = None,
    ):
        try:
            import ollama  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "ollama python package not installed. Run: pip install ollama"
            ) from e

        self._ollama = ollama
        self._client = ollama.Client(
            host=base_url or settings.ollama_base_url,
            timeout=timeout or settings.ollama_timeout_seconds,
        )
        self.model = model or settings.ollama_model
        self.temperature = temperature if temperature is not None else settings.ollama_temperature
        self.num_ctx = num_ctx or settings.ollama_num_ctx

    # ------------------------------------------------------------------
    # Tool-calling mode (preferred — qwen2.5, llama3.1, hermes3 all support)
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AgentStep:
        """One round-trip to Ollama. Returns assistant text + any tool calls."""
        opts = {
            "temperature": temperature if temperature is not None else self.temperature,
            "num_ctx": self.num_ctx,
        }
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "options": opts,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            resp = self._client.chat(**kwargs)
        except Exception as exc:
            logger.warning("Ollama chat failed: %s", exc)
            raise

        msg = resp.get("message", {}) or {}
        content = msg.get("content", "") or ""
        tool_calls: list[ToolCall] = []

        # Native tool_calls field
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) or {}
            name = fn.get("name")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            if name:
                tool_calls.append(ToolCall(name=name, arguments=args))

        # Fallback: parse a JSON tool call out of `content` for models without
        # native tool support. Accepted shape:
        #   {"tool": "name", "arguments": {...}}
        if not tool_calls and content.strip().startswith("{"):
            parsed = _safe_loads(content)
            if isinstance(parsed, dict) and "tool" in parsed:
                tool_calls.append(ToolCall(
                    name=str(parsed["tool"]),
                    arguments=dict(parsed.get("arguments") or {}),
                ))

        return AgentStep(content=content, tool_calls=tool_calls)

    # ------------------------------------------------------------------
    # Raw JSON-mode chat (used for critique agent + final structured output)
    # ------------------------------------------------------------------
    def chat_json(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Ask for strict JSON (no tools). Returns the parsed dict."""
        resp = self._client.chat(
            model=self.model,
            messages=messages,
            format="json",
            options={
                "temperature": temperature if temperature is not None else self.temperature,
                "num_ctx": self.num_ctx,
            },
        )
        content = (resp.get("message") or {}).get("content", "") or "{}"
        return _safe_loads(content) or {}


def _safe_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Try to find first { ... } block
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None
