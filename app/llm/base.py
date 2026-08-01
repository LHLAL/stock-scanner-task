"""LLM client abstractions.

This package provides a provider-agnostic interface for calling LLMs.
The first implementation is OllamaClient (OpenAI-compatible /v1/chat/completions),
which works for both the local daemon AND ollama.com cloud.

Adding a new provider (OpenAI, Anthropic, etc.) only requires subclassing
LLMClient — no changes to analyzer.py / digest.py.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional


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
    """

    @abstractmethod
    def chat(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float = 0.1,
        response_format: Optional[Dict[str, str]] = None,
        max_tokens: Optional[int] = None,
        timeout: int = 30,
    ) -> str:
        """Send messages, return complete response text.

        response_format example: {"type": "json_object"}
        """
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the provider is reachable and the requested model exists."""
        raise NotImplementedError

    @abstractmethod
    def list_models(self) -> List[str]:
        """Return available model IDs."""
        raise NotImplementedError