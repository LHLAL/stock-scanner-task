"""Tests for app.news.analyzer: keyword scoring, TokenBucket, OllamaAnalyzer (mocked)."""
import os
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.news.analyzer import OllamaAnalyzer, TokenBucket, keyword_score
from app.news.models import RawNews


class TestKeywordScore:
    def test_high_impact_positive(self):
        assert keyword_score("央行降准 0.5%", "释放流动性") >= 1.0

    def test_regulator_positive(self):
        assert keyword_score("证监会检查", "") >= 1.0

    def test_economic_data_positive(self):
        assert keyword_score("GDP 增长", "") >= 0.5

    def test_negative_keyword_lowers_score(self):
        score = keyword_score("邀请参加直播活动", "")
        assert score < 0.0

    def test_no_match_returns_zero(self):
        assert keyword_score("普通新闻标题", "普通内容") == 0.0

    def test_empty_title_content(self):
        assert keyword_score("", "") == 0.0

    def test_content_within_200_chars_considered(self):
        assert keyword_score("标题", "央行降准 " + ("x" * 200)) >= 1.0

    def test_content_beyond_200_chars_ignored(self):
        # keyword only in content beyond 200 chars → not detected
        padding = "x" * 300
        assert keyword_score("标题", padding + "央行降准") == 0.0

    def test_max_keyword_weight_returned(self):
        # '央行' (1.0) and '抽奖' (-0.8) both present → max wins
        score = keyword_score("央行降准 抽奖活动", "")
        assert score == 1.0


class TestTokenBucket:
    def test_acquire_does_not_block_first_call(self):
        bucket = TokenBucket(rate_per_minute=60)
        t0 = time.time()
        bucket.acquire()
        assert time.time() - t0 < 0.1

    def test_second_call_blocks_until_interval(self):
        bucket = TokenBucket(rate_per_minute=60)
        bucket.acquire()
        t0 = time.time()
        bucket.acquire()
        elapsed = time.time() - t0
        assert elapsed >= 0.9

    def test_higher_rate_shorter_interval(self):
        bucket = TokenBucket(rate_per_minute=300)
        bucket.acquire()
        t0 = time.time()
        bucket.acquire()
        elapsed = time.time() - t0
        assert elapsed < 0.5


def _sample_news():
    return RawNews(id=1, title="央行降准", content="降准0.5%", ctime=100)


class TestOllamaAnalyzerInit:
    def test_no_key_uses_no_auth(self):
        a = OllamaAnalyzer()
        headers = a._headers()
        assert "Authorization" not in headers

    def test_key_from_constructor(self):
        a = OllamaAnalyzer(api_key="test_key")
        assert a._api_key == "test_key"
        assert a._headers()["Authorization"] == "Bearer test_key"

    def test_key_from_env_var(self):
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "env_key"}):
            a = OllamaAnalyzer()
        assert a._api_key == "env_key"

    def test_key_constructor_overrides_env(self):
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "env_key"}):
            a = OllamaAnalyzer(api_key="ctor_key")
        assert a._api_key == "ctor_key"

    def test_key_from_ollama_key_env(self):
        with patch.dict(os.environ, {"OLLAMA_KEY": "alt_key"}, clear=True):
            a = OllamaAnalyzer()
        assert a._api_key == "alt_key"


class TestOllamaAnalyzerHealthCheck:
    def test_health_check_succeeds_when_model_present(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "models": [{"name": "minimax-m2.5:cloud"}]
            }
            mock_get.return_value.raise_for_status = MagicMock()
            assert OllamaAnalyzer().health_check() is True

    def test_health_check_fails_when_model_missing(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "models": [{"name": "other-model:latest"}]
            }
            mock_get.return_value.raise_for_status = MagicMock()
            assert OllamaAnalyzer().health_check() is False

    def test_health_check_handles_exception(self):
        with patch("requests.get", side_effect=Exception("network")):
            assert OllamaAnalyzer().health_check() is False


class TestOllamaAnalyzerAnalyze:
    def test_returns_none_on_network_error(self):
        with patch("requests.post", side_effect=requests.RequestException("network")):
            assert OllamaAnalyzer().analyze(_sample_news()) is None

    def test_returns_none_on_401(self):
        resp = MagicMock()
        resp.status_code = 401
        with patch("requests.post", return_value=resp):
            a = OllamaAnalyzer()
            assert a.analyze(_sample_news()) is None
            assert a._auth_failed is True

    def test_skips_after_auth_failed(self):
        a = OllamaAnalyzer()
        a._auth_failed = True
        with patch("requests.post") as mock_post:
            assert a.analyze(_sample_news()) is None
            assert not mock_post.called

    def test_returns_parsed_analysis(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "response": '{"summary": "降准利好", "sectors": ["银行"], '
                        '"stocks": ["sh601398"], "direction": "bullish", '
                        '"confidence": 0.85, "rationale": "释放流动性"}'
        }
        with patch("requests.post", return_value=resp):
            analysis = OllamaAnalyzer().analyze(_sample_news())
        assert analysis is not None
        assert analysis.summary == "降准利好"
        assert analysis.confidence == 0.85
        assert analysis.news_hash == _sample_news().hash

    def test_returns_none_on_bad_json(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"response": "not valid json"}
        with patch("requests.post", return_value=resp):
            assert OllamaAnalyzer().analyze(_sample_news()) is None

    def test_uses_correct_model_in_payload(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"response": '{"summary": "", "confidence": 0.5}'}
        with patch("requests.post", return_value=resp) as mock_post:
            OllamaAnalyzer(model="custom:7b").analyze(_sample_news())
            payload = mock_post.call_args.kwargs["json"]
            assert payload["model"] == "custom:7b"
            assert payload["format"] == "json"
            assert payload["stream"] is False

    def test_prompt_includes_news_content(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"response": '{"summary": "", "confidence": 0.5}'}
        with patch("requests.post", return_value=resp) as mock_post:
            OllamaAnalyzer().analyze(_sample_news())
            prompt = mock_post.call_args.kwargs["json"]["prompt"]
            assert "央行降准" in prompt
            assert "降准0.5%" in prompt

    def test_truncates_long_content(self):
        long_content = "x" * 5000
        news = RawNews(id=1, title="t", content=long_content, ctime=0)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"response": '{"summary": "", "confidence": 0.5}'}
        with patch("requests.post", return_value=resp) as mock_post:
            OllamaAnalyzer().analyze(news)
            prompt = mock_post.call_args.kwargs["json"]["prompt"]
            assert long_content[:1000] in prompt
            assert long_content not in prompt