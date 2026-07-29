"""Tests for app.news.models: RawNews + NewsAnalysis.from_json."""
import pytest

from app.news.models import NewsAnalysis, RawNews


class TestRawNews:
    def test_minimal_construction(self):
        n = RawNews(id=1, title="t", content="c", ctime=100)
        assert n.id == 1
        assert n.hash == ""

    def test_published_at_is_float(self):
        n = RawNews(id=1, title="t", content="c", ctime=1785234699)
        assert n.published_at == 1785234699.0


class TestNewsAnalysisFromJson:
    def test_full_valid(self):
        raw = (
            '{"summary": "央行降准 0.5%", "sectors": ["银行", "地产"], '
            '"stocks": ["sh601398"], "direction": "bullish", '
            '"confidence": 0.85, "time_horizon": "next_day", '
            '"rationale": "释放流动性"}'
        )
        a = NewsAnalysis.from_json("abc", raw)
        assert a.news_hash == "abc"
        assert a.summary == "央行降准 0.5%"
        assert a.sectors == ["银行", "地产"]
        assert a.stocks == ["sh601398"]
        assert a.direction == "bullish"
        assert a.confidence == 0.85
        assert a.time_horizon == "next_day"
        assert a.rationale == "释放流动性"

    def test_empty_object_uses_defaults(self):
        a = NewsAnalysis.from_json("h1", "{}")
        assert a.summary == ""
        assert a.sectors == []
        assert a.stocks == []
        assert a.direction == "neutral"
        assert a.confidence == 0.0

    def test_confidence_clamped_high(self):
        a = NewsAnalysis.from_json("h", '{"confidence": 1.5}')
        assert a.confidence == 1.0

    def test_confidence_clamped_low(self):
        a = NewsAnalysis.from_json("h", '{"confidence": -0.3}')
        assert a.confidence == 0.0

    def test_confidence_clamped_zero(self):
        a = NewsAnalysis.from_json("h", '{"confidence": 0.0}')
        assert a.confidence == 0.0

    def test_summary_truncated(self):
        long_summary = "x" * 200
        a = NewsAnalysis.from_json("h", f'{{"summary": "{long_summary}"}}')
        assert len(a.summary) == 80

    def test_sectors_capped(self):
        a = NewsAnalysis.from_json(
            "h",
            '{"sectors": ["a", "b", "c", "d", "e", "f", "g"]}',
        )
        assert len(a.sectors) == 6

    def test_stocks_capped(self):
        a = NewsAnalysis.from_json(
            "h",
            '{"stocks": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"]}',
        )
        assert len(a.stocks) == 10

    def test_direction_lowercased(self):
        a = NewsAnalysis.from_json("h", '{"direction": "BULLISH"}')
        assert a.direction == "bullish"

    def test_accepts_dict_not_just_string(self):
        a = NewsAnalysis.from_json("h", {"summary": "x", "confidence": 0.5})
        assert a.summary == "x"
        assert a.confidence == 0.5


class TestNewsAnalysisProperties:
    def test_is_high_confidence_bullish(self):
        a = NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.7)
        assert a.is_high_confidence is True

    def test_is_high_confidence_bearish(self):
        a = NewsAnalysis(news_hash="h", summary="", direction="bearish", confidence=0.8)
        assert a.is_high_confidence is True

    def test_not_high_confidence_neutral(self):
        a = NewsAnalysis(news_hash="h", summary="", direction="neutral", confidence=0.9)
        assert a.is_high_confidence is False

    def test_not_high_confidence_low_confidence(self):
        a = NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.69)
        assert a.is_high_confidence is False

    def test_emoji_for_directions(self):
        assert NewsAnalysis(news_hash="h", summary="", direction="bullish").emoji == "🟢"
        assert NewsAnalysis(news_hash="h", summary="", direction="bearish").emoji == "🔴"
        assert NewsAnalysis(news_hash="h", summary="", direction="neutral").emoji == "⚪"

    def test_direction_label_for_directions(self):
        assert "利好" in NewsAnalysis(news_hash="h", summary="", direction="bullish").direction_label
        assert "利空" in NewsAnalysis(news_hash="h", summary="", direction="bearish").direction_label
        assert "中性" in NewsAnalysis(news_hash="h", summary="", direction="neutral").direction_label