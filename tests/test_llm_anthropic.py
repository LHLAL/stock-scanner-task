"""Tests for app.llm.anthropic: AnthropicClient."""
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.llm.anthropic import AnthropicClient
from app.llm.base import ChatMessage, LLMError


def _msg(role, content):
    return ChatMessage(role=role, content=content)


class TestAnthropicInit:
    def test_uses_explicit_api_key(self):
        c = AnthropicClient(api_key="real-key")
        assert c._api_key == "real-key"

    def test_uses_env_var_anthropic(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}):
            c = AnthropicClient()
            assert c._api_key == "env-key"

    def test_uses_env_var_claude(self):
        with patch.dict(os.environ, {"CLAUDE_API_KEY": "env-key"}):
            c = AnthropicClient()
            assert c._api_key == "env-key"

    def test_anthropic_wins_over_claude(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a", "CLAUDE_API_KEY": "c"}):
            c = AnthropicClient()
            assert c._api_key == "a"

    def test_custom_base_url_strips_trailing_slash(self):
        c = AnthropicClient(base_url="https://proxy.example.com/")
        assert c._base_url == "https://proxy.example.com"

    def test_default_model(self):
        c = AnthropicClient()
        assert c._default_model == "claude-3-5-sonnet-20241022"


class TestAnthropicChat:
    def test_sends_messages_to_v1_messages(self):
        c = AnthropicClient(api_key="my-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "ok"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="claude-3-5-sonnet")
            url = mock_post.call_args[0][0]
            assert url == "https://api.anthropic.com/v1/messages"

    def test_sends_correct_headers(self):
        c = AnthropicClient(api_key="my-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "ok"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m")
            headers = mock_post.call_args.kwargs["headers"]
            assert headers["x-api-key"] == "my-key"
            assert headers["anthropic-version"] == "2023-06-01"
            assert headers["content-type"] == "application/json"

    def test_separates_system_message(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "ok"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([
                _msg("system", "you are helpful"),
                _msg("user", "hi"),
            ], model="m")
            payload = mock_post.call_args.kwargs["json"]
            assert payload["system"] == "you are helpful"
            assert payload["messages"] == [{"role": "user", "content": "hi"}]

    def test_no_system_message_works(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "ok"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m")
            payload = mock_post.call_args.kwargs["json"]
            assert "system" not in payload
            assert payload["messages"] == [{"role": "user", "content": "hi"}]

    def test_includes_max_tokens(self):
        c = AnthropicClient(api_key="k", default_max_tokens=2048)
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "ok"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m")
            assert mock_post.call_args.kwargs["json"]["max_tokens"] == 2048

    def test_includes_temperature(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "ok"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m", temperature=0.7)
            assert mock_post.call_args.kwargs["json"]["temperature"] == 0.7

    def test_clamps_temperature_to_anthropic_range(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "ok"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m", temperature=2.0)
            assert mock_post.call_args.kwargs["json"]["temperature"] == 1.0

    def test_json_object_appends_system_instruction(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "ok"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([
                _msg("system", "be brief"),
                _msg("user", "hi"),
            ], model="m", response_format={"type": "json_object"})
            payload = mock_post.call_args.kwargs["json"]
            assert "JSON object only" in payload["system"]
            assert "be brief" in payload["system"]

    def test_returns_text_content(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "world!"},
                ]
            }
            mock_post.return_value.status_code = 200
            assert c.chat([_msg("user", "hi")], model="m") == "Hello world!"

    def test_skips_non_text_blocks(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [
                    {"type": "tool_use", "id": "x"},
                    {"type": "text", "text": "result"},
                ]
            }
            mock_post.return_value.status_code = 200
            assert c.chat([_msg("user", "hi")], model="m") == "result"

    def test_no_user_message_raises(self):
        c = AnthropicClient(api_key="k")
        with pytest.raises(LLMError, match="at least one user"):
            c.chat([_msg("system", "you are helpful")], model="m")

    def test_no_api_key_raises(self):
        c = AnthropicClient()  # no key
        c._api_key = None
        with pytest.raises(LLMError, match="api_key not configured"):
            c.chat([_msg("user", "hi")], model="m")

    def test_auth_failure_raises(self):
        c = AnthropicClient(api_key="bad")
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 401
            mock_post.return_value.text = "Unauthorized"
            with pytest.raises(LLMError, match="auth failed"):
                c.chat([_msg("user", "hi")], model="m")

    def test_network_error_raises(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post", side_effect=requests.ConnectionError("nope")):
            with pytest.raises(LLMError):
                c.chat([_msg("user", "hi")], model="m")

    def test_invalid_json_raises(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.side_effect = ValueError
            mock_post.return_value.text = "not json"
            mock_post.return_value.status_code = 200
            with pytest.raises(LLMError, match="invalid JSON"):
                c.chat([_msg("user", "hi")], model="m")

    def test_uses_cache(self):
        # Verifies the inherited base-class cache works
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "content": [{"type": "text", "text": "first"}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "x")], model="m")
            c.chat([_msg("user", "x")], model="m")  # cached
            assert mock_post.call_count == 1
            c.chat([_msg("user", "y")], model="m")  # different msg
            assert mock_post.call_count == 2


class TestAnthropicHealthCheck:
    def test_returns_false_when_no_key(self):
        c = AnthropicClient()
        c._api_key = None
        assert c.health_check() is False

    def test_returns_true_on_200(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            assert c.health_check() is True

    def test_returns_true_on_400(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 400
            assert c.health_check() is True

    def test_returns_false_on_network_error(self):
        c = AnthropicClient(api_key="k")
        with patch("requests.post", side_effect=Exception("nope")):
            assert c.health_check() is False


class TestAnthropicListModels:
    def test_returns_default_catalogue(self):
        c = AnthropicClient(api_key="k")
        models = c.list_models()
        assert "claude-3-5-sonnet-20241022" in models
        assert "claude-3-5-haiku-20241022" in models
        assert "claude-3-opus-20240229" in models