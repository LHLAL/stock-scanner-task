"""Tests for app.news.digest: DigestFetcher + DigestAnalyzer + dataclasses."""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.news.digest import (
    Digest,
    DigestAnalysis,
    DigestAnalyzer,
    DigestFetcher,
    _parse_digest_date,
    _parse_digest_type,
)


def _make_digest(id=1, title="财联社7月29日晚间新闻精选", content="...", ctime=1785332636):
    return Digest(
        id=id,
        title=title,
        digest_type=_parse_digest_type(title),
        digest_date=_parse_digest_date(ctime),
        ctime=ctime,
        content=content,
    )


class TestDigestParsing:
    def test_parse_type_morning(self):
        assert _parse_digest_type("财联社7月29日早间新闻精选") == "morning"

    def test_parse_type_noon(self):
        assert _parse_digest_type("财联社7月29日午间新闻精选") == "noon"

    def test_parse_type_evening(self):
        assert _parse_digest_type("财联社7月29日晚间新闻精选") == "evening"

    def test_parse_type_unknown(self):
        assert _parse_digest_type("财联社7月29日全天新闻精选") == "unknown"

    def test_parse_date_utc8(self):
        ctime = 1785332636  # corresponds to 2026-07-29 21:43 UTC+8
        date = _parse_digest_date(ctime)
        assert date == "2026-07-29"

    def test_digest_hash_is_stable(self):
        d = _make_digest(content="hello")
        h1 = d.hash
        d2 = Digest(id=1, title=d.title, digest_type=d.digest_type,
                     digest_date=d.digest_date, ctime=d.ctime, content="hello")
        assert d2.hash == h1


class TestDigestAnalysis:
    def test_from_json_valid(self):
        raw = """{
            "summary": "市场震荡偏弱，关注政策面",
            "market_sentiment": "bearish",
            "market_confidence": 0.75,
            "sector_impacts": [
                {"sector": "半导体", "direction": "bullish", "magnitude": "high", "reason": "国产替代加速"}
            ],
            "holdings_impacts": [
                {"code": "sh601398", "name": "工商银行", "impact": "neutral",
                 "confidence": 0.6, "reason": "银行业整体稳定"}
            ],
            "key_events": ["央行降准", "美光业绩超预期"],
            "narrative_themes": ["AI算力", "国产替代"],
            "rationale": "综合来看市场情绪偏弱"
        }"""
        a = DigestAnalysis.from_json(["h1", "h2"], raw)
        assert a.market_sentiment == "bearish"
        assert a.market_confidence == 0.75
        assert len(a.sector_impacts) == 1
        assert a.sector_impacts[0]["sector"] == "半导体"
        assert len(a.holdings_impacts) == 1
        assert a.has_holdings_impact is False  # neutral, not positive/negative
        assert a.strongest_holdings_impact is None

    def test_from_json_clamping(self):
        raw = '{"market_confidence": 2.0}'
        a = DigestAnalysis.from_json([], raw)
        assert a.market_confidence == 1.0

    def test_from_json_empty(self):
        a = DigestAnalysis.from_json([], "{}")
        assert a.market_sentiment == "neutral"
        assert a.market_confidence == 0.0
        assert a.sector_impacts == []

    def test_has_holdings_impact(self):
        a = DigestAnalysis(
            digest_hashes=[],
            summary="",
            holdings_impacts=[{"code": "sh601398", "impact": "positive", "confidence": 0.7}],
        )
        assert a.has_holdings_impact is True
        assert a.strongest_holdings_impact is not None

    def test_no_holdings_impact_when_neutral(self):
        a = DigestAnalysis(
            digest_hashes=[],
            summary="",
            holdings_impacts=[{"code": "sh601398", "impact": "neutral", "confidence": 0.5}],
        )
        assert a.has_holdings_impact is False


class TestDigestFetcher:
    def _make_response(self, items):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"list": items, "total": len(items)}
        resp.raise_for_status = MagicMock()
        return resp

    def test_fetch_empty(self):
        resp = self._make_response([])
        with patch("requests.post", return_value=resp):
            f = DigestFetcher()
            assert f.fetch() == []

    def test_fetch_parses_items(self):
        items = [
            {"id": 1, "title": "财联社7月29日早间新闻精选", "ctime": 1785283807,
             "content": "1、测试内容"},
            {"id": 2, "title": "财联社7月29日午间新闻精选", "ctime": 1785297742,
             "content": "2、测试内容"},
        ]
        # First call returns data, second returns empty (terminates pagination)
        resp_data = self._make_response(items)
        resp_empty = self._make_response([])
        with patch("requests.post", side_effect=[resp_data, resp_empty]) as mock_post:
            f = DigestFetcher()
            digests = f.fetch()
            assert len(digests) == 2
            assert digests[0].digest_type == "morning"
            assert digests[1].digest_type == "noon"
            assert digests[0].digest_date == "2026-07-29"

    def test_fetch_handles_network_error(self):
        import requests as req
        with patch("requests.post", side_effect=req.RequestException("network")):
            f = DigestFetcher()
            assert f.fetch() == []

    def test_fetch_handles_invalid_json(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=resp):
            f = DigestFetcher()
            assert f.fetch() == []

    def test_fetch_uses_post_with_json_body(self):
        items = [{"id": 1, "title": "财联社7月29日晚间新闻精选",
                  "ctime": 1785332636, "content": "test"}]
        resp = self._make_response(items)
        with patch("requests.post", return_value=resp) as mock_post:
            DigestFetcher().fetch()
            args = mock_post.call_args
            assert "kwarg" in str(args) or "json" in str(args)
            json_body = args.kwargs.get("json") or args[1].get("json")
            assert json_body["keyword"] == "新闻精选"
            assert json_body["category"] == "red"

    def test_fetch_pagination_updates_last_time(self):
        items = [
            {"id": 1, "title": "财联社7月29日早间新闻精选", "ctime": 1785283807, "content": "x"},
            {"id": 2, "title": "财联社7月29日午间新闻精选", "ctime": 1785297742, "content": "y"},
        ]
        resp = self._make_response(items)
        with patch("requests.post", return_value=resp):
            f = DigestFetcher()
            f.fetch()
            assert f._last_time == 1785283807

    def test_fetch_passes_no_proxy(self):
        items = [{"id": 1, "title": "财联社7月29日早间新闻精选",
                  "ctime": 1785283807, "content": "x"}]
        resp = self._make_response(items)
        with patch("requests.post", return_value=resp) as mock_post:
            DigestFetcher().fetch()
            assert mock_post.call_args.kwargs["proxies"] == {"http": "", "https": ""}

    def test_fetch_includes_referer(self):
        items = [{"id": 1, "title": "财联社7月29日早间新闻精选",
                  "ctime": 1785283807, "content": "x"}]
        resp = self._make_response(items)
        with patch("requests.post", return_value=resp) as mock_post:
            DigestFetcher().fetch()
            headers = mock_post.call_args.kwargs["headers"]
            assert "cls.cn" in headers.get("referer", "")


class TestDigestAnalyzer:
    def test_analyze_empty_returns_none(self):
        llm = MagicMock()
        a = DigestAnalyzer(llm)
        assert a.analyze([], []) is None

    def test_analyze_calls_llm_and_parses(self):
        llm = MagicMock()
        llm.chat.return_value = '{"summary": "ok", "market_sentiment": "bullish", "market_confidence": 0.7, "sector_impacts": [], "holdings_impacts": []}'
        a = DigestAnalyzer(llm)
        result = a.analyze([_make_digest()], [])
        assert result is not None
        assert result.market_sentiment == "bullish"
        llm.chat.assert_called_once()

    def test_analyze_includes_holdings_in_prompt(self):
        llm = MagicMock()
        llm.chat.return_value = '{"market_confidence": 0.5}'
        from app.news.models import Stock
        a = DigestAnalyzer(llm)
        a.analyze([_make_digest()], [Stock(code="sh601398", name="工商银行")])
        prompt = llm.chat.call_args[0][0][1].content
        assert "sh601398" in prompt
        assert "工商银行" in prompt

    def test_analyze_returns_none_on_llm_failure(self):
        llm = MagicMock()
        llm.chat.return_value = None
        a = DigestAnalyzer(llm)
        assert a.analyze([_make_digest()], []) is None

    def test_analyze_returns_none_on_invalid_json(self):
        llm = MagicMock()
        llm.chat.return_value = "not json"
        a = DigestAnalyzer(llm)
        assert a.analyze([_make_digest()], []) is None

    def test_analyze_includes_digest_content(self):
        llm = MagicMock()
        llm.chat.return_value = '{"market_confidence": 0.5}'
        a = DigestAnalyzer(llm)
        a.analyze([_make_digest(content="央行降准0.5%释放流动性")], [])
        prompt = llm.chat.call_args[0][0][1].content
        assert "央行降准0.5%释放流动性" in prompt

class TestHoldingsTableFormat:
    """Input to LLM should be markdown table, not bullet list."""

    def test_holdings_with_codes_format_as_table(self):
        from app.news.digest import DigestAnalyzer, Digest
        from app.news.models import Stock
        d = Digest(id=1, title="t", digest_type="morning",
                   digest_date="2026-07-30", ctime=100, content="x")
        llm = MagicMock()
        llm.chat.return_value = (
            '{"summary": "x", "market_sentiment": "bullish", "market_confidence": 0.7}'
        )
        analyzer = DigestAnalyzer(llm)
        holdings = [
            Stock(code="sh601398", name="工商银行"),
            Stock(code="sh600028", name="中国石化"),
        ]
        analyzer.analyze([d], holdings)
        prompt = llm.chat.call_args[0][0][1].content
        assert "| 代码 | 名称 |" in prompt
        assert "| sh601398 | 工商银行 |" in prompt
        assert "| sh600028 | 中国石化 |" in prompt
        # Make sure old bullet format is NOT used
        assert "- sh601398 工商银行" not in prompt

    def test_holdings_empty_shows_placeholder(self):
        from app.news.digest import DigestAnalyzer, Digest
        d = Digest(id=1, title="t", digest_type="morning",
                   digest_date="2026-07-30", ctime=100, content="x")
        llm = MagicMock()
        llm.chat.return_value = '{"summary": "x"}'
        analyzer = DigestAnalyzer(llm)
        analyzer.analyze([d], [])
        prompt = llm.chat.call_args[0][0][1].content
        assert "（无持仓）" in prompt
