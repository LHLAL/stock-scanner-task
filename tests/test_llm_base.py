"""Tests for app.llm.base: LLMClient ABC + cache."""
import time
from unittest.mock import MagicMock

import pytest

from app.llm.base import ChatMessage, LLMClient, LLMError


class _FakeClient(LLMClient):
    """Concrete impl for testing the cache logic."""
    def __init__(self, ttl: float = 300):
        super().__init__(cache_ttl_seconds=ttl)
        self.calls = 0

    def _do_chat(self, messages, model, temperature=0.1, response_format=None,
                 max_tokens=None, timeout=30):
        self.calls += 1
        return f"result_{self.calls}"

    def health_check(self, model=None):
        return True

    def list_models(self):
        return []


def _msg(role, content):
    return ChatMessage(role=role, content=content)


class TestCacheKey:
    def test_same_inputs_same_key(self):
        c = _FakeClient()
        msgs = [_msg("user", "hi")]
        k1 = c._cache_key("m", msgs, 0.1, None, None)
        k2 = c._cache_key("m", msgs, 0.1, None, None)
        assert k1 == k2

    def test_different_model_different_key(self):
        c = _FakeClient()
        msgs = [_msg("user", "hi")]
        k1 = c._cache_key("a", msgs, 0.1, None, None)
        k2 = c._cache_key("b", msgs, 0.1, None, None)
        assert k1 != k2

    def test_different_temperature_different_key(self):
        c = _FakeClient()
        msgs = [_msg("user", "hi")]
        k1 = c._cache_key("m", msgs, 0.1, None, None)
        k2 = c._cache_key("m", msgs, 0.5, None, None)
        assert k1 != k2

    def test_different_messages_different_key(self):
        c = _FakeClient()
        k1 = c._cache_key("m", [_msg("user", "hi")], 0.1, None, None)
        k2 = c._cache_key("m", [_msg("user", "bye")], 0.1, None, None)
        assert k1 != k2

    def test_different_response_format_different_key(self):
        c = _FakeClient()
        msgs = [_msg("user", "hi")]
        k1 = c._cache_key("m", msgs, 0.1, None, None)
        k2 = c._cache_key("m", msgs, 0.1, {"type": "json_object"}, None)
        assert k1 != k2

    def test_different_max_tokens_different_key(self):
        c = _FakeClient()
        msgs = [_msg("user", "hi")]
        k1 = c._cache_key("m", msgs, 0.1, None, 500)
        k2 = c._cache_key("m", msgs, 0.1, None, 1000)
        assert k1 != k2

    def test_different_roles_same_content_different_key(self):
        c = _FakeClient()
        k1 = c._cache_key("m", [_msg("user", "x")], 0.1, None, None)
        k2 = c._cache_key("m", [_msg("system", "x")], 0.1, None, None)
        assert k1 != k2


class TestCacheGetSet:
    def test_set_then_get(self):
        c = _FakeClient()
        c._cache_set("k", "v")
        assert c._cache_get("k") == "v"

    def test_get_missing_returns_none(self):
        c = _FakeClient()
        assert c._cache_get("nope") is None

    def test_expired_entry_returned_none_and_removed(self):
        c = _FakeClient(ttl=0.01)
        c._cache_set("k", "v")
        time.sleep(0.05)
        assert c._cache_get("k") is None
        assert "k" not in c._cache

    def test_clear_cache(self):
        c = _FakeClient()
        c._cache_set("a", "1")
        c._cache_set("b", "2")
        c.clear_cache()
        assert c._cache == {}


class TestChatCaching:
    def test_first_call_uses_do_chat(self):
        c = _FakeClient()
        result = c.chat([_msg("user", "hi")], "m")
        assert result == "result_1"
        assert c.calls == 1

    def test_repeat_call_returns_cache(self):
        c = _FakeClient()
        msgs = [_msg("user", "hi")]
        c.chat(msgs, "m")
        c.chat(msgs, "m")
        c.chat(msgs, "m")
        assert c.calls == 1  # only one underlying call

    def test_different_args_trigger_new_call(self):
        c = _FakeClient()
        c.chat([_msg("user", "hi")], "m")
        c.chat([_msg("user", "bye")], "m")
        c.chat([_msg("user", "hi")], "n")
        assert c.calls == 3

    def test_expired_cache_re_fetches(self):
        c = _FakeClient(ttl=0.01)
        c.chat([_msg("user", "hi")], "m")
        time.sleep(0.05)
        c.chat([_msg("user", "hi")], "m")
        assert c.calls == 2  # first cached, second re-fetched

    def test_abc_chat_uses_cache_wrapper(self):
        c = _FakeClient()
        assert hasattr(c, "chat")
        assert hasattr(c, "_do_chat")
        first = c.chat([_msg("user", "test")], "m")
        second = c.chat([_msg("user", "test")], "m")
        assert first == second
        assert c.calls == 1


class TestClearCache:
    def test_clear_via_clear_method(self):
        c = _FakeClient()
        c.chat([_msg("user", "x")], "m")
        c.chat([_msg("user", "y")], "m")
        c.clear_cache()
        c.chat([_msg("user", "x")], "m")
        assert c.calls == 3  # all 3 calls because cache was cleared