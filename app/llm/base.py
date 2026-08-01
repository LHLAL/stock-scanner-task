"""LLM client abstractions.

This package provides a provider-agnostic interface for calling LLMs.
The first implementation is OllamaClient (OpenAI-compatible /v1/chat/completions),
which works for both the local daemon AND ollama.com cloud.

Adding a new provider (OpenAI, Anthropic, etc.) only requires subclassing
LLMClient — no changes to analyzer.py / digest.py.
"""
import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class ChatMessage:
    role: str   # "system" | "user" | "assistant"
    content: str


class LLMError(Exception):
    """Raised when LLM call fails. Caller decides whether to retry / give up."""


class LLMClient(ABC):
    """Abstract base for all LLM providers.

    All implementations must use the OpenAI-compatible chat format
    (system + user messages) so prompts and logic are portable.

    Built-in response cache: identical (model, messages, params) calls
    within `cache_ttl_seconds` return the cached response. Override
    `_do_chat` to implement the actual network call; subclasses don't
    need to know about caching.
    """

    def __init__(self, cache_ttl_seconds: float = 300):
        self._cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Tuple[float, str]] = {}

    def chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 0.1,
        response_format: Optional[Dict[str, str]] = None,
        max_tokens: Optional[int] = None,
        timeout: int = 30,
    ) -> str:
        """Cached wrapper around `_do_chat`. Override `_do_chat` in subclasses."""
        key = self._cache_key(model, messages, temperature, response_format, max_tokens)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        result = self._do_chat(
            messages, model, temperature, response_format, max_tokens, timeout
        )
        if result is not None:
            self._cache_set(key, result)
        return result

    @abstractmethod
    def _do_chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 0.1,
        response_format: Optional[Dict[str, str]] = None,
        max_tokens: Optional[int] = None,
        timeout: int = 30,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def health_check(self, model: Optional[str] = None) -> bool:
        """Return True if the provider is reachable and the model exists."""
        raise NotImplementedError

    @abstractmethod
    def list_models(self) -> List[str]:
        """Return available model IDs."""
        raise NotImplementedError

    def _cache_key(
        self,
        model: str,
        messages: List[ChatMessage],
        temperature: float,
        response_format: Optional[Dict[str, str]],
        max_tokens: Optional[int],
    ) -> str:
        h = hashlib.sha256()
        h.update(model.encode())
        h.update(str(temperature).encode())
        h.update(repr(response_format).encode())
        h.update(str(max_tokens).encode())
        for m in messages:
            h.update(m.role.encode())
            h.update(b"\x00")
            h.update(m.content.encode())
            h.update(b"\x00")
        return h.hexdigest()

    def _cache_get(self, key: str) -> Optional[str]:
        if key not in self._cache:
            return None
        ts, val = self._cache[key]
        if time.time() - ts < self._cache_ttl:
            return val
        del self._cache[key]
        return None

    def _cache_set(self, key: str, val: str) -> None:
        self._cache[key] = (time.time(), val)

    def clear_cache(self) -> None:
        """Drop all cached responses."""
        self._cache.clear()