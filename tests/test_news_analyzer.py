"""Tests for app.news.analyzer: keyword scoring, TokenBucket, OllamaAnalyzer.

The OllamaAnalyzer is now a thin wrapper over LLMClient. HTTP-level
tests are in test_ollama_client.py.
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from app.news.analyzer import (
    NEWS_SYSTEM_PROMPT,
    NEWS_USER_TEMPLATE,
    OllamaAnalyzer,
    TokenBucket,
    keyword_score,
)
from app.news.models import NewsAnalysis, RawNews, Stock
from app.llm.base import LLMError, ChatMessage


def _sample_news():
    return RawNews(
        id=1,
        title="央行降准",
        content="降准0.5%",
        ctime=1785234699,
        hash="abc123",
    )


class TestKeywordScore:
    def test_high_impact_positive(self):
        assert keyword_score("央行降准 0.5%", "释放流动性") >= 1.0

    def test_regulator_positive(self):
        assert keyword_score("证监会检查", "") >= 1.0

    def test_economic_data_positive(self):
        assert keyword_score("GDP 增长", "") >= 0.5

    def test_negative_keyword_lowers_score(self):
        assert keyword_score("邀请参加直播活动", "") <= 0.0

    def test_no_match_returns_zero(self):
        assert keyword_score("普通新闻标题", "普通内容") == 0.0

    def test_empty_title_content(self):
        assert keyword_score("", "") == 0.0

    def test_content_within_200_chars_considered(self):
        assert keyword_score("标题", "央行降准 " + ("x" * 200)) >= 1.0

    def test_content_beyond_200_chars_ignored(self):
        padding = "x" * 300
        assert keyword_score("标题", padding + "央行降准") == 0.0

    def test_max_keyword_weight_returned(self):
        assert keyword_score("央行降准 抽奖活动", "") == 1.0


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


def _make_analyzer(client=None):
    return OllamaAnalyzer(
        client=client or MagicMock(),
        model="test:model",
    )


class TestOllamaAnalyzerInit:
    def test_stores_client_and_model(self):
        a = OllamaAnalyzer(client=MagicMock(), model="test:model")
        assert a._model == "test:model"


class TestOllamaAnalyzerHealthCheck:
    def test_delegates_to_client(self):
        client = MagicMock()
        client.list_models.return_value = ["test:model", "other:1b"]
        a = OllamaAnalyzer(client=client, model="test:model")
        assert a.health_check() is True
        client.list_models.assert_called_once()

    def test_returns_false_when_model_missing(self):
        client = MagicMock()
        client.list_models.return_value = ["other:1b"]
        a = OllamaAnalyzer(client=client, model="test:model")
        assert a.health_check() is False

    def test_returns_false_on_exception(self):
        client = MagicMock()
        client.list_models.side_effect = Exception("network")
        a = OllamaAnalyzer(client=client, model="test:model")
        assert a.health_check() is False


class TestOllamaAnalyzerAnalyze:
    def test_returns_none_when_client_raises(self):
        client = MagicMock()
        client.chat.side_effect = LLMError("network down")
        a = OllamaAnalyzer(client=client, model="test:model")
        assert a.analyze(_sample_news()) is None

    def test_marks_auth_failed_on_auth_error(self):
        client = MagicMock()
        client.chat.side_effect = LLMError("auth failed (401): bad key")
        a = OllamaAnalyzer(client=client, model="test:model")
        a.analyze(_sample_news())
        assert a._auth_failed is True

    def test_skips_call_after_auth_failed(self):
        client = MagicMock()
        a = OllamaAnalyzer(client=client, model="test:model")
        a._auth_failed = True
        a.analyze(_sample_news())
        client.chat.assert_not_called()

    def test_returns_parsed_analysis(self):
        client = MagicMock()
        client.chat.return_value = (
            '{"summary": "降准利好", "sectors": ["银行"], '
            '"stocks": [{"code": "sh601398", "name": "工商银行"}], '
            '"direction": "bullish", "confidence": 0.85, '
            '"time_horizon": "intraday", "rationale": "释放流动性"}'
        )
        a = OllamaAnalyzer(client=client, model="test:model")
        analysis = a.analyze(_sample_news())
        assert analysis is not None
        assert analysis.confidence == 0.85
        client.chat.assert_called_once()

    def test_returns_none_on_bad_json(self):
        client = MagicMock()
        client.chat.return_value = "not valid json"
        a = OllamaAnalyzer(client=client, model="test:model")
        assert a.analyze(_sample_news()) is None

    def test_uses_messages_with_system_and_user(self):
        client = MagicMock()
        client.chat.return_value = '{"summary": "x", "direction": "bullish", "confidence": 0.5}'
        a = OllamaAnalyzer(client=client, model="custom:7b")
        a.analyze(_sample_news())
        messages = client.chat.call_args[0][0]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"
        assert "custom:7b" in str(client.chat.call_args)

    def test_prompt_includes_news_content(self):
        client = MagicMock()
        client.chat.return_value = '{"summary": "x", "direction": "bullish", "confidence": 0.5}'
        a = OllamaAnalyzer(client=client, model="test:model")
        a.analyze(_sample_news())
        user_msg = client.chat.call_args[0][0][1]
        assert "央行降准" in user_msg.content
        assert "降准0.5%" in user_msg.content

    def test_truncates_long_content(self):
        long_content = "x" * 5000
        news = RawNews(id=1, title="t", content=long_content, ctime=0)
        client = MagicMock()
        client.chat.return_value = '{"summary": "x", "direction": "bullish", "confidence": 0.5}'
        a = OllamaAnalyzer(client=client, model="test:model")
        a.analyze(news)
        user_msg = client.chat.call_args[0][0][1]
        assert long_content[:1000] in user_msg.content
        assert long_content not in user_msg.content

    def test_uses_json_object_format(self):
        client = MagicMock()
        client.chat.return_value = '{"summary": "x", "direction": "bullish", "confidence": 0.5}'
        a = OllamaAnalyzer(client=client, model="test:model")
        a.analyze(_sample_news())
        rf = client.chat.call_args.kwargs.get("response_format") or client.chat.call_args[1].get("response_format")
        assert rf == {"type": "json_object"}

    def test_uses_correct_model(self):
        client = MagicMock()
        client.chat.return_value = '{"summary": "x", "direction": "bullish", "confidence": 0.5}'
        OllamaAnalyzer(client=client, model="custom:7b").analyze(_sample_news())
        assert client.chat.call_args.kwargs.get("model") == "custom:7b" or client.chat.call_args[1].get("model") == "custom:7b"