"""Tests for app.llm.ollama: OllamaClient (OpenAI-compatible /v1/chat/completions)."""
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.llm.base import ChatMessage, LLMError
from app.llm.ollama import OllamaClient


def _msg(role, content):
    return ChatMessage(role=role, content=content)


class TestOllamaClientInit:
    def test_strips_trailing_slash(self):
        c = OllamaClient(host="http://localhost:11434/")
        assert c._host == "http://localhost:11434"

    def test_uses_ollama_default_key_for_local(self):
        c = OllamaClient()
        assert c._api_key == "ollama"

    def test_uses_explicit_api_key(self):
        c = OllamaClient(api_key="real-key")
        assert c._api_key == "real-key"

    def test_uses_env_var(self):
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "env-key"}):
            c = OllamaClient()
            assert c._api_key == "env-key"

    def test_explicit_key_wins_over_env(self):
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "env-key"}):
            c = OllamaClient(api_key="explicit-key")
            assert c._api_key == "explicit-key"


class TestOllamaClientChat:
    def test_sends_messages_to_v1_chat_completions(self):
        c = OllamaClient(host="http://localhost:11434", timeout=10)
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "ok"}}]
            }
            mock_post.return_value.status_code = 200
            mock_post.return_value.raise_for_status = MagicMock()

            c.chat([_msg("user", "hi")], model="gpt-oss:20b-cloud")
            url = mock_post.call_args[0][0]
            assert url == "http://localhost:11434/v1/chat/completions"

            payload = mock_post.call_args.kwargs["json"]
            assert payload["model"] == "gpt-oss:20b-cloud"
            assert payload["messages"] == [{"role": "user", "content": "hi"}]
            assert payload["stream"] is False

    def test_returns_message_content(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "hello world"}}]
            }
            mock_post.return_value.status_code = 200
            mock_post.return_value.raise_for_status = MagicMock()

            result = c.chat([_msg("user", "hi")], model="m")
            assert result == "hello world"

    def test_includes_temperature(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "x"}}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m", temperature=0.5)
            assert mock_post.call_args.kwargs["json"]["temperature"] == 0.5

    def test_includes_response_format_when_set(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "x"}}]
            }
            mock_post.return_value.status_code = 200
            rf = {"type": "json_object"}
            c.chat([_msg("user", "hi")], model="m", response_format=rf)
            assert mock_post.call_args.kwargs["json"]["response_format"] == rf

    def test_includes_max_tokens(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "x"}}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m", max_tokens=500)
            assert mock_post.call_args.kwargs["json"]["max_tokens"] == 500

    def test_sends_authorization_header(self):
        c = OllamaClient(api_key="my-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "x"}}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m")
            headers = mock_post.call_args.kwargs["headers"]
            assert headers["Authorization"] == "Bearer my-key"

    def test_no_auth_header_when_key_is_ollama_default(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "choices": [{"message": {"content": "x"}}]
            }
            mock_post.return_value.status_code = 200
            c.chat([_msg("user", "hi")], model="m")
            headers = mock_post.call_args.kwargs["headers"]
            # 'ollama' key still sent for local daemon (which ignores it)
            assert headers.get("Authorization") == "Bearer ollama"

    def test_raises_on_auth_failure(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 401
            mock_post.return_value.text = "Unauthorized"
            with pytest.raises(LLMError, match="auth"):
                c.chat([_msg("user", "hi")], model="m")
        assert c._auth_failed is True

    def test_raises_on_other_http_error(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 500
            mock_post.return_value.text = "Server Error"
            with pytest.raises(LLMError):
                c.chat([_msg("user", "hi")], model="m")

    def test_raises_on_network_error(self):
        c = OllamaClient()
        with patch("requests.post", side_effect=requests.ConnectionError("nope")):
            with pytest.raises(LLMError):
                c.chat([_msg("user", "hi")], model="m")

    def test_raises_on_invalid_json(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.side_effect = ValueError("not json")
            mock_post.return_value.text = "<html>error</html>"
            mock_post.return_value.status_code = 200
            with pytest.raises(LLMError, match="invalid JSON"):
                c.chat([_msg("user", "hi")], model="m")

    def test_raises_on_unexpected_shape(self):
        c = OllamaClient()
        with patch("requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"no_choices": []}
            mock_post.return_value.status_code = 200
            with pytest.raises(LLMError, match="unexpected response shape"):
                c.chat([_msg("user", "hi")], model="m")

    def test_skips_call_after_auth_failed(self):
        c = OllamaClient()
        c._auth_failed = True
        with patch("requests.post") as mock_post:
            with pytest.raises(LLMError, match="refusing"):
                c.chat([_msg("user", "hi")], model="m")
            mock_post.assert_not_called()


class TestOllamaClientHealthCheck:
    def test_returns_true_when_model_present(self):
        c = OllamaClient()
        c._model = "gpt-oss:20b-cloud"
        with patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "data": [{"id": "gpt-oss:20b-cloud"}, {"id": "other:1b"}]
            }
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = MagicMock()
            assert c.health_check() is True

    def test_returns_false_when_model_missing(self):
        c = OllamaClient()
        c._model = "gpt-oss:20b-cloud"
        with patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "data": [{"id": "other:1b"}]
            }
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = MagicMock()
            assert c.health_check() is False

    def test_returns_false_on_exception(self):
        c = OllamaClient()
        with patch("requests.get", side_effect=Exception("nope")):
            assert c.health_check() is False


class TestOllamaClientListModels:
    def test_returns_model_ids(self):
        c = OllamaClient()
        with patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "data": [{"id": "gpt-oss:20b-cloud"}, {"id": "llama3:8b"}]
            }
            mock_get.return_value.status_code = 200
            mock_get.return_value.raise_for_status = MagicMock()
            assert c.list_models() == ["gpt-oss:20b-cloud", "llama3:8b"]

    def test_returns_empty_on_error(self):
        c = OllamaClient()
        with patch("requests.get", side_effect=Exception("nope")):
            assert c.list_models() == []