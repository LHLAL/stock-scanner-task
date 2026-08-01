"""Anthropic Claude client (POST /v1/messages).

Supports Claude 3.x models via Anthropic's API. The chat interface
parses the system message separately (Claude uses a top-level
'system' field, not a system-role message) and maps the response.
"""
import logging
import os
from typing import Dict, List, Optional

import requests

from app.llm.base import ChatMessage, LLMClient, LLMError

logger = logging.getLogger(__name__)

# Default model list to advertise in list_models().
# Anthropic's own /v1/models endpoint is the source of truth, but
# the caller can pass an explicit list in self._extra_models.
_DEFAULT_MODELS = [
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
]


class AnthropicClient(LLMClient):
    """Anthropic Claude client.

    Uses api_key from constructor, env var ANTHROPIC_API_KEY, or auth.
    The base class chat() handles caching automatically — this client
    only implements _do_chat() with the network call.
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: str = "claude-3-5-sonnet-20241022",
        default_max_tokens: int = 4096,
        timeout: int = 60,
        cache_ttl_seconds: float = 300,
    ):
        super().__init__(cache_ttl_seconds=cache_ttl_seconds)
        self._api_key = (
            api_key
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_API_KEY")
        )
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._default_model = default_model
        self._default_max_tokens = default_max_tokens
        self._timeout = timeout

    def _headers(self) -> dict:
        if not self._api_key:
            raise LLMError("Anthropic api_key not configured")
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _do_chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 0.1,
        response_format: Optional[Dict[str, str]] = None,
        max_tokens: Optional[int] = None,
        timeout: int = 60,
    ) -> str:
        # Anthropic separates system message from the message list
        system_parts = [m.content for m in messages if m.role == "system"]
        user_msgs = [
            {"role": m.role, "content": m.content}
            for m in messages if m.role != "system"
        ]
        if not user_msgs:
            raise LLMError("Anthropic requires at least one user/assistant message")

        # Anthropic expects max_tokens; required parameter
        if max_tokens is None:
            max_tokens = self._default_max_tokens

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": user_msgs,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if temperature is not None:
            # Anthropic allows 0.0..1.0
            payload["temperature"] = max(0.0, min(1.0, float(temperature)))
        # Anthropic uses tool-use for JSON mode; for our schema-based
        # extraction, we just rely on prompt-based JSON. Future: could
        # add a tool definition when response_format={"type": "json_object"}.
        if response_format and response_format.get("type") == "json_object":
            payload["system"] = (
                (payload.get("system") or "")
                + "\n\nIMPORTANT: respond with a single valid JSON object only, "
                "no markdown fences, no extra prose."
            )

        url = f"{self._base_url}/v1/messages"
        try:
            resp = requests.post(
                url, headers=self._headers(), json=payload, timeout=timeout
            )
        except requests.RequestException as e:
            raise LLMError(f"network error: {e}") from e

        if resp.status_code in (401, 403):
            raise LLMError(f"auth failed ({resp.status_code}): check api_key")
        if resp.status_code != 200:
            raise LLMError(
                f"http {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"invalid JSON: {resp.text[:200]}") from e

        try:
            # Anthropic response: {"content": [{"type": "text", "text": "..."}, ...]}
            content_blocks = data.get("content", [])
            texts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
            return "".join(texts) or ""
        except (KeyError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {data}") from e

    def health_check(self, model: Optional[str] = None) -> bool:
        """Anthropic doesn't expose a /v1/models list endpoint as cleanly.
        Return True if we can reach the API and have a configured key.
        """
        if not self._api_key:
            return False
        try:
            resp = requests.post(
                f"{self._base_url}/v1/messages",
                headers=self._headers(),
                json={
                    "model": self._default_model,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=10,
            )
            # 200 = reachable; 400 = reachable but bad params (also ok)
            return resp.status_code in (200, 400)
        except Exception as e:
            logger.debug(f"AnthropicClient health check failed: {e}")
            return False

    def list_models(self) -> List[str]:
        # Anthropic has no /v1/models endpoint; return the default catalogue
        return list(_DEFAULT_MODELS)