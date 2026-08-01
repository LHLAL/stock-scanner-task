"""Ollama client (OpenAI-compatible /v1/chat/completions).

Works for:
- Local daemon: host="http://localhost:11434", api_key="ollama" (default ignored)
- Ollama Cloud: host="https://ollama.com", api_key=OLLAMA_API_KEY
"""
import logging
import os
from typing import List, Optional

import requests

from app.llm.base import ChatMessage, LLMClient, LLMError

logger = logging.getLogger(__name__)


class OllamaClient(LLMClient):
    def __init__(
        self,
        host: str = "http://localhost:11434",
        api_key: Optional[str] = None,
        timeout: int = 30,
        cache_ttl_seconds: int = 300,
    ):
        super().__init__(cache_ttl_seconds=cache_ttl_seconds)
        self._host = host.rstrip("/")
        # Local daemon ignores api_key (accepts any value), cloud requires real key
        self._api_key = (
            api_key
            or os.environ.get("OLLAMA_API_KEY")
            or "ollama"  # local daemon default
        )
        self._timeout = timeout
        self._auth_failed = False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _do_chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 0.1,
        response_format: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        timeout: int = 30,
    ) -> str:
        if self._auth_failed:
            raise LLMError("auth failed previously, refusing to call")

        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        url = f"{self._host}/v1/chat/completions"
        try:
            resp = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise LLMError(f"network error: {e}") from e

        if resp.status_code in (401, 403):
            self._auth_failed = True
            raise LLMError(
                f"auth failed ({resp.status_code}): check api_key"
            )
        if resp.status_code != 200:
            raise LLMError(
                f"http {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"invalid JSON: {resp.text[:200]}") from e

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected response shape: {data}") from e

    def health_check(self, model: Optional[str] = None) -> bool:
        try:
            models = self.list_models()
            check = model or "gpt-oss:20b-cloud"
            available = any(check in m for m in models)
            if not available:
                logger.warning(
                    f"[OllamaClient] model {check} not in {models[:5]}"
                )
            return available
        except Exception as e:
            logger.debug(f"OllamaClient health check failed: {e}")
            return False

    def list_models(self) -> List[str]:
        try:
            resp = requests.get(
                f"{self._host}/v1/models",
                headers=self._headers(),
                timeout=5,
            )
            resp.raise_for_status()
            return [m.get("id", "") for m in resp.json().get("data", [])]
        except Exception:
            return []