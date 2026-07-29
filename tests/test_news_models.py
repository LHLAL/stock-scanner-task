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

class TestEnhancedFields:
    def test_defaults_on_construction(self):
        a = NewsAnalysis(news_hash="h", summary="")
        assert a.news_category == "general"
        assert a.bottleneck_order_signal == "none"
        assert a.bottleneck_capacity_signal == "none"
        assert a.bottleneck_margin_signal == "unknown"
        assert a.is_kneck is False
        assert a.scarcity_pillars == []
        assert a.trend_horizon_years == 1
        assert a.industry_certainty == "speculative"
        assert a.narrative_themes == []

    def test_from_json_parses_enhanced(self):
        raw = {
            "summary": "AI 算力芯片订单爆发",
            "sectors": ["半导体"],
            "direction": "bullish",
            "confidence": 0.9,
            "news_category": "order",
            "bottleneck_order_signal": "strong",
            "bottleneck_capacity_signal": "utilization_high",
            "bottleneck_margin_signal": "rising",
            "is_kneck": True,
            "scarcity_pillars": ["tech_moat", "single_point"],
            "trend_horizon_years": 5,
            "industry_certainty": "established",
            "narrative_themes": ["AI算力", "国产替代"],
        }
        a = NewsAnalysis.from_json("h", raw)
        assert a.news_category == "order"
        assert a.bottleneck_order_signal == "strong"
        assert a.bottleneck_capacity_signal == "utilization_high"
        assert a.bottleneck_margin_signal == "rising"
        assert a.is_kneck is True
        assert a.scarcity_pillars == ["tech_moat", "single_point"]
        assert a.trend_horizon_years == 5
        assert a.industry_certainty == "established"
        assert a.narrative_themes == ["AI算力", "国产替代"]

    def test_invalid_news_category_falls_back_to_general(self):
        a = NewsAnalysis.from_json("h", {"news_category": "garbage"})
        assert a.news_category == "general"

    def test_invalid_order_signal_falls_back(self):
        a = NewsAnalysis.from_json("h", {"bottleneck_order_signal": "garbage"})
        assert a.bottleneck_order_signal == "none"

    def test_invalid_capacity_signal_falls_back(self):
        a = NewsAnalysis.from_json("h", {"bottleneck_capacity_signal": "garbage"})
        assert a.bottleneck_capacity_signal == "none"

    def test_invalid_margin_signal_falls_back(self):
        a = NewsAnalysis.from_json("h", {"bottleneck_margin_signal": "garbage"})
        assert a.bottleneck_margin_signal == "unknown"

    def test_invalid_scarcity_pillar_filtered(self):
        a = NewsAnalysis.from_json(
            "h",
            {"scarcity_pillars": ["tech_moat", "fake_pillar", "single_point"]},
        )
        assert a.scarcity_pillars == ["tech_moat", "single_point"]

    def test_trend_horizon_clamped_high(self):
        a = NewsAnalysis.from_json("h", {"trend_horizon_years": 50})
        assert a.trend_horizon_years == 10

    def test_trend_horizon_clamped_low(self):
        a = NewsAnalysis.from_json("h", {"trend_horizon_years": 0})
        assert a.trend_horizon_years == 1

    def test_trend_horizon_default(self):
        a = NewsAnalysis.from_json("h", {})
        assert a.trend_horizon_years == 1

    def test_invalid_certainty_falls_back(self):
        a = NewsAnalysis.from_json("h", {"industry_certainty": "garbage"})
        assert a.industry_certainty == "speculative"

    def test_narrative_themes_capped(self):
        a = NewsAnalysis.from_json(
            "h",
            {"narrative_themes": [f"theme{i}" for i in range(10)]},
        )
        assert len(a.narrative_themes) == 5

    def test_narrative_themes_truncated_each(self):
        a = NewsAnalysis.from_json("h", {"narrative_themes": ["x" * 50]})
        assert len(a.narrative_themes[0]) == 20


class TestBottleneckProperties:
    def test_is_bottleneck_signal_true_when_kneck(self):
        a = NewsAnalysis(news_hash="h", summary="", is_kneck=True)
        assert a.is_bottleneck_signal is True

    def test_is_bottleneck_signal_true_when_strong_order(self):
        a = NewsAnalysis(news_hash="h", summary="", bottleneck_order_signal="strong")
        assert a.is_bottleneck_signal is True

    def test_is_bottleneck_signal_true_when_capacity_high(self):
        a = NewsAnalysis(news_hash="h", summary="", bottleneck_capacity_signal="utilization_high")
        assert a.is_bottleneck_signal is True

    def test_is_bottleneck_signal_true_when_margin_rising(self):
        a = NewsAnalysis(news_hash="h", summary="", bottleneck_margin_signal="rising")
        assert a.is_bottleneck_signal is True

    def test_is_bottleneck_signal_false_with_weak_signals(self):
        a = NewsAnalysis(news_hash="h", summary="",
                         bottleneck_order_signal="mentioned",
                         bottleneck_capacity_signal="none",
                         bottleneck_margin_signal="stable")
        assert a.is_bottleneck_signal is False

    def test_is_bottleneck_signal_false_default(self):
        a = NewsAnalysis(news_hash="h", summary="")
        assert a.is_bottleneck_signal is False

    def test_badge_includes_kneck(self):
        a = NewsAnalysis(news_hash="h", summary="", is_kneck=True)
        assert "卡脖子" in a.badge

    def test_badge_includes_strong_order(self):
        a = NewsAnalysis(news_hash="h", summary="", bottleneck_order_signal="strong")
        assert "订单" in a.badge

    def test_badge_includes_rising_margin(self):
        a = NewsAnalysis(news_hash="h", summary="", bottleneck_margin_signal="rising")
        assert "毛利" in a.badge

    def test_badge_includes_full_capacity(self):
        a = NewsAnalysis(news_hash="h", summary="", bottleneck_capacity_signal="utilization_high")
        assert "满产" in a.badge

    def test_badge_empty_when_no_signals(self):
        a = NewsAnalysis(news_hash="h", summary="")
        assert a.badge == ""

    def test_badge_combines_multiple(self):
        a = NewsAnalysis(news_hash="h", summary="",
                         is_kneck=True,
                         bottleneck_order_signal="strong")
        parts = a.badge.split()
        assert len(parts) == 2

    def test_kness_pillars_label_chinese(self):
        a = NewsAnalysis(news_hash="h", summary="",
                         scarcity_pillars=["tech_moat", "single_point", "certification"])
        assert "技术代差" in a.kness_pillars_label
        assert "单点刚需" in a.kness_pillars_label
        assert "3-5年认证" in a.kness_pillars_label

    def test_category_emoji(self):
        assert NewsAnalysis(news_hash="h", summary="", news_category="policy").category_emoji == "📜"
        assert NewsAnalysis(news_hash="h", summary="", news_category="order").category_emoji == "📦"
        assert NewsAnalysis(news_hash="h", summary="", news_category="general").category_emoji == "📰"
        assert NewsAnalysis(news_hash="h", summary="", news_category="unknown").category_emoji == "📰"
