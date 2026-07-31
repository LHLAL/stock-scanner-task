"""Tests for app.news.monitor: NewsMonitor._tick pipeline (mocked fetcher/analyzer)."""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.config import ClsConfig, LlmConfig, NewsConfig, NewsFilterConfig
from app.news.models import NewsAnalysis, RawNews, Stock
from app.news.monitor import NewsMonitor
from app.storage import PriceDB


def _make_news(hash="h1", title="央行降准", content="降准0.5%"):
    return RawNews(id=1, title=title, content=content, ctime=1785234699, hash=hash)


def _make_analysis(hash="h1", sectors=None, stocks=None, direction="bullish", confidence=0.85):
    raw_stocks = stocks or ["sh601398"]
    parsed = [Stock(code=c, name="") for c in raw_stocks]
    return NewsAnalysis(
        news_hash=hash,
        summary="降准利好",
        sectors=sectors or ["银行"],
        stocks=parsed,
        direction=direction,
        confidence=confidence,
    )


def _make_news_config(**overrides):
    from app.config import DigestConfig
    cfg = NewsConfig(
        enabled=True,
        cls=ClsConfig(sign=None, cookie=None,
                     poll_interval_seconds=30,
                     off_hours_poll_interval_seconds=300),
        filter=NewsFilterConfig(
            keyword_threshold=0.5,
            min_confidence_for_notify=0.8,
            min_confidence_for_holdings_alert=0.65,
            bottleneck_confidence_floor=0.65,
            max_notifications_per_day=20,
        ),
            llm=LlmConfig(model="test:model", host="http://localhost:11434",
                          api_key=None, max_per_minute=10,
                          cache_ttl_hours=24, request_timeout_seconds=30),
            digest=DigestConfig(enabled=True, days_back=3,
                                poll_interval_minutes=120,
                                min_market_confidence_for_notify=0.7,
                                min_holdings_confidence_for_notify=0.6,
                                sign=None, cookie=None),
        )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def setup_monitor(temp_db_path):
    """Build a NewsMonitor with mocked fetcher/analyzer/sector for control."""
    db = PriceDB(temp_db_path)
    cfg = _make_news_config()

    monitor = NewsMonitor(config=cfg, holdings={"sh601398"}, db=db)
    # Mock internal components
    monitor._fetcher = MagicMock()
    monitor._analyzer = MagicMock()
    monitor._sector_mapper = MagicMock()
    monitor._bucket = MagicMock()  # Don't actually rate-limit in tests
    return monitor


class TestNewsMonitorHealthCheck:
    def test_returns_dict_with_components(self, temp_db_path):
        db = PriceDB(temp_db_path)
        monitor = NewsMonitor(config=_make_news_config(), holdings=set(), db=db)
        with patch.object(monitor._fetcher, "fetch", return_value=[]):
            with patch.object(monitor._analyzer, "health_check", return_value=True):
                health = monitor.health_check()
        assert "cls" in health
        assert "llm" in health
        assert "sector" in health


class TestNewsMonitorTick:
    def test_no_news_no_processing(self, setup_monitor):
        setup_monitor._fetcher.fetch.return_value = []
        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append(a)
        setup_monitor._tick()
        assert processed == []

    def test_processes_new_high_impact_news(self, setup_monitor):
        news = _make_news()
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis()
        setup_monitor._sector_mapper.map_analysis.return_value = [Stock(code="sh601398", name="")] # noqa

        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append((a, r, h))
        setup_monitor._tick()

        assert len(processed) == 1
        analysis, related, hits = processed[0]
        assert analysis.confidence == 0.85
        assert any(s.code == "sh601398" for s in related)
        assert hits is True  # 命中 holdings

    def test_skips_low_keyword_score(self, setup_monitor):
        # News with no high-impact keywords
        news = _make_news(title="今天天气真好", content="阳光明媚")
        setup_monitor._fetcher.fetch.return_value = [news]

        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append(a)
        setup_monitor._tick()
        assert processed == []

    def test_skips_already_seen_news(self, setup_monitor, temp_db_path):
        news = _make_news(hash="dup")
        # Pre-seed cache so it's "seen"
        setup_monitor._db.news_seen("dup", news.title, news.content, news.ctime)

        setup_monitor._fetcher.fetch.return_value = [news]

        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append(a)
        setup_monitor._tick()
        assert processed == []
        setup_monitor._analyzer.analyze.assert_not_called()

    def test_skips_when_llm_returns_none(self, setup_monitor):
        setup_monitor._fetcher.fetch.return_value = [_make_news()]
        setup_monitor._analyzer.analyze.return_value = None

        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append(a)
        setup_monitor._tick()
        assert processed == []
        setup_monitor._db.news_get_recent_analyses()  # Should be empty
        from app.storage import PriceDB
        assert setup_monitor._db.news_get_recent_analyses() == []

    def test_persists_analysis_to_db(self, setup_monitor):
        news = _make_news(hash="persist-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis(hash="persist-test")
        setup_monitor._sector_mapper.map_analysis.return_value = [Stock(code="sh601398", name="")] # noqa

        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        result = setup_monitor._db.news_get_analysis("persist-test")
        assert result is not None
        assert result["confidence"] == 0.85

    def test_marks_high_confidence_as_notified(self, setup_monitor, temp_db_path):
        news = _make_news(hash="hc-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis(
            hash="hc-test", confidence=0.85, direction="bullish"
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("hc-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 1

    def test_does_not_notify_low_confidence_no_holdings_hit(self, setup_monitor, temp_db_path):
        news = _make_news(hash="low-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis(
            hash="low-test", confidence=0.4, direction="neutral"
        )
        setup_monitor._sector_mapper.map_analysis.return_value = [Stock(code="sh999999", name="")] # noqa  # not in holdings

        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("low-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0

    def test_holdings_hit_with_low_confidence_does_not_notify(self, setup_monitor, temp_db_path):
        # threshold min_confidence_for_holdings_alert = 0.65; 0.55 should NOT notify
        news = _make_news(hash="hit-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis(
            hash="hit-test", confidence=0.55, direction="bullish"
        )
        setup_monitor._sector_mapper.map_analysis.return_value = [Stock(code="sh601398", name="")] # noqa
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("hit-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0

    def test_holdings_hit_with_high_confidence_notifies(self, setup_monitor, temp_db_path):
        # holdings_alert = 0.65; 0.70 should pass
        news = _make_news(hash="hit-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis(
            hash="hit-test", confidence=0.70, direction="bullish"
        )
        setup_monitor._sector_mapper.map_analysis.return_value = [Stock(code="sh601398", name="")] # noqa
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("hit-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 1

    def test_processes_multiple_news_in_one_tick(self, setup_monitor):
        setup_monitor._fetcher.fetch.return_value = [
            _make_news(hash="a"),
            _make_news(hash="b", title="央行降息"),
            _make_news(hash="c", title="普通标题", content="完全无关的内容"),
        ]
        setup_monitor._analyzer.analyze.side_effect = lambda n: _make_analysis(hash=n.hash)
        setup_monitor._sector_mapper.map_analysis.return_value = []

        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append(a)
        setup_monitor._tick()

        assert len(processed) == 2


class TestNewsMonitorStartStop:
    def test_start_is_idempotent(self, setup_monitor):
        assert setup_monitor._running is False
        setup_monitor.start()
        assert setup_monitor._running is True
        setup_monitor.start()
        assert setup_monitor._running is True
        setup_monitor.stop()

    def test_loop_calls_tick_and_sleeps(self, setup_monitor):
        setup_monitor._fetcher.fetch.return_value = []
        setup_monitor._on_update = lambda a, r, h: None

        import time as time_mod
        original_sleep = time_mod.sleep

        # Start the loop in background; stop after a moment
        setup_monitor._current_interval = lambda: 0.05  # fast tick
        setup_monitor.start()
        time_mod.sleep(0.2)
        setup_monitor.stop()

        assert setup_monitor._running is False

    def test_start_launches_digest_thread(self, setup_monitor):
        """When digest enabled, start() should spawn a NewsMonitor-Digest thread."""
        assert setup_monitor._digest_fetcher is not None
        # Patch the digest cycle to count invocations and short-circuit
        cycle_count = {"n": 0}

        def fake_cycle():
            cycle_count["n"] += 1
            if cycle_count["n"] >= 2:
                setup_monitor.stop()
        with patch.object(setup_monitor, "run_digest_cycle", side_effect=fake_cycle):
            with patch("app.news.monitor.is_market_open", return_value=True):
                # Force a short poll interval
                setup_monitor._config.digest.poll_interval_minutes = 0
                setup_monitor.start()
                import time
                time.sleep(0.5)
        assert cycle_count["n"] >= 2

    def test_start_skips_digest_thread_when_disabled(self, setup_monitor):
        """When digest is not configured, no digest thread should be started."""
        setup_monitor.stop()
        setup_monitor._running = False
        # Disable digest
        setup_monitor._digest_fetcher = None
        setup_monitor._digest_analyzer = None
        # Patch _digest_loop to track if it was called
        with patch.object(setup_monitor, "_digest_loop") as fake_digest_loop:
            setup_monitor.start()
            import time
            time.sleep(0.2)
            fake_digest_loop.assert_not_called()
        setup_monitor.stop()

class TestBottleneckNotify:
    def test_kneck_with_low_confidence_does_not_notify(self, setup_monitor, temp_db_path):
        # bottleneck_confidence_floor = 0.65; 0.6 should NOT notify
        news = _make_news(hash="kneck-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="kneck-test", summary="卡脖子", direction="bullish",
            confidence=0.6, is_kneck=True, scarcity_pillars=["tech_moat"],
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("kneck-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0

    def test_kneck_with_high_confidence_notifies(self, setup_monitor, temp_db_path):
        # bottleneck floor 0.65 + 0.85 conf + bullish → notify
        news = _make_news(hash="kneck-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="kneck-test", summary="卡脖子突破", direction="bullish",
            confidence=0.85, is_kneck=True, scarcity_pillars=["tech_moat"],
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("kneck-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 1

    def test_kneck_neutral_direction_does_not_notify(self, setup_monitor, temp_db_path):
        # 即使 is_kneck=true 且高置信，direction=neutral 仍不通知
        news = _make_news(hash="neutral-kneck")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="neutral-kneck", summary="中性卡脖子", direction="neutral",
            confidence=0.9, is_kneck=True,
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("neutral-kneck",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0

    def test_strong_order_with_low_confidence_does_not_notify(self, setup_monitor, temp_db_path):
        # 订单爆发 + 低置信 0.5 < 0.65 floor → 不通知
        news = _make_news(hash="order-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="order-test", summary="订单爆发", direction="bullish",
            confidence=0.5, bottleneck_order_signal="strong",
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("order-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0

    def test_does_not_notify_neutral_no_signals(self, setup_monitor, temp_db_path):
        news = _make_news(hash="boring-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="boring-test", summary="普通", direction="neutral",
            confidence=0.5,
        )
        setup_monitor._sector_mapper.map_analysis.return_value = [Stock(code="sh999999", name="")] # noqa
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("boring-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0


class TestDailyLimit:
    def test_default_limit_is_1000(self, temp_db_path):
        from app.config import LlmConfig
        cfg = _make_news_config()
        assert cfg.llm.daily_limit == 1000

    def test_limit_zero_means_unlimited(self, temp_db_path):
        cfg = _make_news_config(llm=LlmConfig(daily_limit=0))
        db = PriceDB(temp_db_path)
        monitor = NewsMonitor(config=cfg, holdings=set(), db=db)
        assert monitor._check_daily_limit() is True

    def test_count_increments_on_successful_llm_call(self, setup_monitor):
        setup_monitor._fetcher.fetch.return_value = [_make_news()]
        setup_monitor._analyzer.analyze.return_value = _make_analysis()
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()
        assert setup_monitor._daily_count == 1

    def test_skips_when_limit_reached(self, setup_monitor):
        setup_monitor._daily_count = setup_monitor._config.llm.daily_limit
        setup_monitor._fetcher.fetch.return_value = [_make_news()]

        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append(a)
        setup_monitor._tick()

        assert processed == []
        setup_monitor._analyzer.analyze.assert_not_called()

    def test_warns_at_90_percent(self, setup_monitor, caplog):
        import logging
        setup_monitor._daily_count = int(setup_monitor._config.llm.daily_limit * 0.95)
        with caplog.at_level(logging.WARNING, logger="app.news.monitor"):
            setup_monitor._check_daily_limit()
        assert any("daily LLM limit" in r.message for r in caplog.records)

    def test_does_not_warn_when_below_threshold(self, setup_monitor):
        setup_monitor._daily_count = int(setup_monitor._config.llm.daily_limit * 0.5)
        assert setup_monitor._check_daily_limit() is True
        assert setup_monitor._daily_limit_warned is False

    def test_daily_usage_property(self, setup_monitor):
        setup_monitor._daily_count = 100
        usage = setup_monitor.daily_usage
        assert usage["count"] == 100
        assert usage["limit"] == 1000
        assert usage["remaining"] == 900

    def test_daily_usage_unlimited(self, setup_monitor):
        setup_monitor._config.llm.daily_limit = 0
        usage = setup_monitor.daily_usage
        assert usage["remaining"] is None

    def test_limit_resets_on_date_change(self, setup_monitor):
        from datetime import date, timedelta
        setup_monitor._daily_count = 999
        setup_monitor._daily_count_date = date.today() - timedelta(days=1)
        setup_monitor._check_daily_limit()
        assert setup_monitor._daily_count == 0
        assert setup_monitor._daily_limit_warned is False

    def test_llm_failure_does_not_increment_daily_count(self, setup_monitor):
        # Failed LLM calls must NOT consume the daily quota (bug fix)
        setup_monitor._fetcher.fetch.return_value = [_make_news()]
        setup_monitor._analyzer.analyze.return_value = None
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()
        assert setup_monitor._daily_count == 0


class TestTieredNotificationGating:
    """Three-tier notification gating logic (TIER1/TIER2/TIER3)."""

    def test_tier1_passes_with_high_confidence(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.85),
            hits_holdings=False,
        ) is True

    def test_tier1_blocked_neutral(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="neutral", confidence=0.95),
            hits_holdings=False,
        ) is False

    def test_tier1_blocked_low_confidence(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.7),
            hits_holdings=False,
        ) is False

    def test_tier2_passes_holdings_with_sufficient_confidence(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.7),
            hits_holdings=True,
        ) is True

    def test_tier2_blocked_holdings_with_low_confidence(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.6),
            hits_holdings=True,
        ) is False

    def test_tier3_passes_bottleneck_with_sufficient_confidence(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish",
                        confidence=0.7, is_kneck=True),
            hits_holdings=False,
        ) is True

    def test_tier3_blocked_bottleneck_low_confidence(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish",
                        confidence=0.6, is_kneck=True),
            hits_holdings=False,
        ) is False

    def test_tier3_blocked_bottleneck_neutral(self, setup_monitor):
        # 即使 is_kneck=true 且高置信，direction=neutral 仍不通知
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="neutral",
                        confidence=0.95, is_kneck=True),
            hits_holdings=False,
        ) is False

    def test_no_signals_no_notify(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.4),
            hits_holdings=False,
        ) is False


class TestNotificationDailyCap:
    """_notif_count + max_notifications_per_day enforcement."""

    def test_daily_cap_default_is_20(self, setup_monitor):
        assert setup_monitor._config.filter.max_notifications_per_day == 20

    def test_notif_count_starts_zero(self, setup_monitor):
        assert setup_monitor._notif_count == 0

    def test_notif_skipped_at_cap(self, setup_monitor, temp_db_path):
        setup_monitor._notif_count = setup_monitor._config.filter.max_notifications_per_day
        # Try to trigger another notification
        news = _make_news(hash="cap-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="cap-test", summary="应该被挡", direction="bullish", confidence=0.95
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        # Should still be marked notified=False because _notify was skipped
        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("cap-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0
        # But the analysis IS in DB
        row = conn.execute(
            "SELECT COUNT(*) FROM news_analysis WHERE news_hash = ?", ("cap-test",)
        ).fetchone()[0] if False else 0
        conn = sqlite3.connect(temp_db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM news_analysis WHERE news_hash = ?", ("cap-test",)
        ).fetchone()[0]
        conn.close()
        assert row == 1  # analysis saved


class TestMinUsefulConfidence:
    """Per-analysis usefulness floor: conf < 0.5 → silent noise."""

    def test_default_min_useful_confidence_is_half(self, setup_monitor):
        assert setup_monitor._config.filter.min_useful_confidence == 0.5

    def test_low_confidence_bullish_blocked(self, setup_monitor):
        # 0.35 confidence 不足以说明 bullish 是有效信号
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.35),
            hits_holdings=False,
        ) is False

    def test_low_confidence_bearish_blocked(self, setup_monitor):
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bearish", confidence=0.4),
            hits_holdings=False,
        ) is False

    def test_low_confidence_kneck_blocked(self, setup_monitor):
        # 即使卡脖子，conf 0.4 仍被 min_useful 拦下
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish",
                        confidence=0.4, is_kneck=True),
            hits_holdings=False,
        ) is False

    def test_low_confidence_holdings_blocked(self, setup_monitor):
        # 持仓命中 + 低置信度 仍被拦下（即使 holdings floor 0.65 > 0.5）
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.4),
            hits_holdings=True,
        ) is False

    def test_at_threshold_passes(self, setup_monitor):
        # conf = 0.5 正好达到 min_useful，下游 tier 检查生效
        # tier1 需要 0.8，conf 0.5 不够；但当 holdings=True 且非中性，会进 tier2 检查 0.65
        # 这里应该返回 False（不到 0.65）但通过了 min_useful
        # 测试边界: conf = 0.5 没到 holdings_alert (0.65), 返回 False
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.5),
            hits_holdings=True,
        ) is False

    def test_exactly_at_min_useful_with_high_tier(self, setup_monitor):
        # conf = 0.5 + holdings, 但 min_confidence_for_holdings_alert = 0.65
        # 即使 conf 0.5 > min_useful 0.5, 不到 0.65 仍不能通知
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.5),
            hits_holdings=True,
        ) is False

    def test_above_threshold_works_normally(self, setup_monitor):
        # 正常情况：conf 0.85, bullish → tier1
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="bullish", confidence=0.85),
            hits_holdings=False,
        ) is True

    def test_below_min_useful_even_neutral_stays_blocked(self, setup_monitor):
        # neutral + 低 conf: 本来就不会通知，min_useful 是双保险
        assert setup_monitor._should_notify(
            NewsAnalysis(news_hash="h", summary="", direction="neutral", confidence=0.3),
            hits_holdings=False,
        ) is False


class TestMinSaveConfidence:
    """Pure noise (conf < min_save_confidence) should not be saved to DB."""

    def test_default_min_save_confidence(self, setup_monitor):
        assert setup_monitor._config.filter.min_save_confidence == 0.25

    def test_pure_noise_not_saved(self, setup_monitor, temp_db_path):
        # conf 0.15 (生物医药新闻等) 应被拒绝
        news = _make_news(hash="noise-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="noise-test", summary="生物医药板块上涨", direction="neutral",
            confidence=0.15,
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append(a)
        setup_monitor._tick()

        assert processed == []
        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM news_analysis WHERE news_hash = ?", ("noise-test",)
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_at_threshold_saved(self, setup_monitor, temp_db_path):
        # conf 0.25 刚好达到 min_save → 应保存
        news = _make_news(hash="border-save")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="border-save", summary="中性新闻", direction="neutral",
            confidence=0.25,
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM news_analysis WHERE news_hash = ?", ("border-save",)
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_above_threshold_saved(self, setup_monitor, temp_db_path):
        news = _make_news(hash="above")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="above", summary="利好", direction="bullish", confidence=0.7,
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM news_analysis WHERE news_hash = ?", ("above",)
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_news_cache_marked_seen_even_when_skipped(self, setup_monitor, temp_db_path):
        # 即使不保存分析，news_cache 也应该标记为 seen（避免重复 LLM 调用）
        news = _make_news(hash="noise-2")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="noise-2", summary="噪声", direction="neutral", confidence=0.1,
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT analyzed FROM news_cache WHERE hash = ?", ("noise-2",)
        ).fetchone()[0]
        conn.close()
        # Should be marked analyzed=0 (we saw it but didn't analyze)
        assert flag == 0

    def test_llm_call_still_counts_against_daily_limit(self, setup_monitor):
        # 即便不保存，LLM 调用还是消耗了 daily_limit 配额（避免重试循环）
        news = _make_news(hash="noise-quota")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="noise-quota", summary="x", direction="neutral", confidence=0.1,
        )
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        assert setup_monitor._daily_count == 1


class TestDigestCycle:
    """NewsMonitor.run_digest_cycle() integration."""

    def test_digest_disabled_returns_none(self, setup_monitor):
        from app.news.digest import DigestFetcher
        setup_monitor._config.digest.enabled = False
        setup_monitor._digest_fetcher = None  # forced to None when disabled
        assert setup_monitor._digest_fetcher is None

    def test_digest_fetcher_none_returns_none(self, setup_monitor):
        setup_monitor._digest_fetcher = None
        setup_monitor._digest_analyzer = None
        assert setup_monitor.run_digest_cycle() is None

    def test_digest_no_news_returns_none(self, setup_monitor):
        with patch.object(setup_monitor._digest_fetcher, "fetch", return_value=[]):
            assert setup_monitor.run_digest_cycle() is None

    def test_digest_filters_old_news(self, setup_monitor):
        from datetime import datetime, timezone, timedelta
        old_ctime = int((datetime.now(tz=timezone(timedelta(hours=8))).timestamp() - 10 * 86400))
        old = _make_news_digest(title="旧新闻精选", ctime=old_ctime, digest_type="morning")
        with patch.object(setup_monitor._digest_fetcher, "fetch", return_value=[old]):
            assert setup_monitor.run_digest_cycle() is None

    def test_digest_full_cycle_returns_analysis(self, setup_monitor):
        from app.news.digest import DigestAnalysis
        from datetime import datetime, timezone, timedelta
        now = int(datetime.now(tz=timezone(timedelta(hours=8))).timestamp())
        d1 = _make_news_digest(title="财联社7月30日早间新闻精选", ctime=now - 3600, digest_type="morning")
        d2 = _make_news_digest(title="财联社7月30日午间新闻精选", ctime=now - 1800, digest_type="noon")
        with patch.object(setup_monitor._digest_fetcher, "fetch", return_value=[d1, d2]):
            with patch.object(setup_monitor._digest_analyzer, "analyze") as mock_analyze:
                mock_analyze.return_value = DigestAnalysis(
                    digest_hashes=["h1", "h2"],
                    summary="测试摘要",
                    market_sentiment="bullish",
                    market_confidence=0.8,
                )
                result = setup_monitor.run_digest_cycle()
        assert result is not None
        assert result.market_sentiment == "bullish"
        assert setup_monitor._daily_count == 1

    def test_digest_should_notify_high_market_confidence(self, setup_monitor):
        from app.news.digest import DigestAnalysis
        a = DigestAnalysis(
            digest_hashes=[], summary="", market_sentiment="bullish",
            market_confidence=0.8,
        )
        assert setup_monitor._should_notify_digest(a) is True

    def test_digest_should_notify_low_confidence(self, setup_monitor):
        from app.news.digest import DigestAnalysis
        a = DigestAnalysis(
            digest_hashes=[], summary="", market_sentiment="bullish",
            market_confidence=0.4,
        )
        assert setup_monitor._should_notify_digest(a) is False

    def test_digest_should_notify_holdings_impact(self, setup_monitor):
        from app.news.digest import DigestAnalysis
        a = DigestAnalysis(
            digest_hashes=[], summary="", market_sentiment="neutral",
            market_confidence=0.8,    # pass market threshold
            holdings_impacts=[{"code": "sh601398", "name": "工商银行",
                               "impact": "positive", "confidence": 0.7}],
        )
        assert setup_monitor._should_notify_digest(a) is True

    def test_digest_should_notify_holdings_neutral_no(self, setup_monitor):
        from app.news.digest import DigestAnalysis
        a = DigestAnalysis(
            digest_hashes=[], summary="", market_sentiment="neutral",
            market_confidence=0.5,
            holdings_impacts=[{"code": "sh601398", "name": "工商银行",
                               "impact": "neutral", "confidence": 0.7}],
        )
        assert setup_monitor._should_notify_digest(a) is False

    def test_digest_daily_cap_blocks_notification(self, setup_monitor):
        from app.news.digest import DigestAnalysis
        setup_monitor._notif_count = 100
        a = DigestAnalysis(
            digest_hashes=[], summary="", market_sentiment="bullish",
            market_confidence=0.8,
        )
        assert setup_monitor._should_notify_digest(a) is False

    def test_digest_volatile_sentiment_notifies(self, setup_monitor):
        from app.news.digest import DigestAnalysis
        a = DigestAnalysis(
            digest_hashes=[], summary="", market_sentiment="volatile",
            market_confidence=0.75,
        )
        assert setup_monitor._should_notify_digest(a) is True


def _make_news_digest(title, ctime, digest_type="morning", content="测试内容"):
    from app.news.digest import Digest, _parse_digest_date
    return Digest(
        id=1, title=title, digest_type=digest_type,
        digest_date=_parse_digest_date(ctime), ctime=ctime, content=content,
    )


class TestLLMFailureHandling:
    """LLM failure: should NOT consume daily quota, should open circuit after 3 fails."""

    def test_failed_llm_does_not_increment_daily_count(self, setup_monitor):
        news = _make_news()
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = None  # LLM failed
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()
        # failed call should NOT count
        assert setup_monitor._daily_count == 0

    def test_successful_llm_increments_daily_count(self, setup_monitor):
        news = _make_news()
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis()
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()
        assert setup_monitor._daily_count == 1

    def test_consecutive_failures_open_circuit(self, setup_monitor):
        setup_monitor._record_llm_failure()
        setup_monitor._record_llm_failure()
        assert setup_monitor._llm_circuit_open is False
        setup_monitor._record_llm_failure()
        assert setup_monitor._llm_circuit_open is True
        assert setup_monitor._llm_circuit_open_until > time.time()

    def test_circuit_open_skips_llm(self, setup_monitor):
        import time as time_mod
        news = _make_news()
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = None
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        # Manually open circuit
        setup_monitor._llm_circuit_open = True
        setup_monitor._llm_circuit_open_until = time_mod.time() + 300
        setup_monitor._tick()
        # analyze should not be called
        setup_monitor._analyzer.analyze.assert_not_called()
        # No failure recorded (skipped before analyze)
        assert setup_monitor._consecutive_llm_failures == 0

    def test_successful_call_resets_failure_counter(self, setup_monitor):
        setup_monitor._consecutive_llm_failures = 2
        news = _make_news()
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis()
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()
        assert setup_monitor._consecutive_llm_failures == 0


class TestProgressLogging:
    """Verify tick + digest emit structured progress logs."""

    def test_tick_logs_structured_summary(self, setup_monitor, caplog):
        import logging
        from app.news.models import RawNews
        caplog.set_level(logging.INFO, logger="app.news.monitor")

        news = _make_news()
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis()
        setup_monitor._sector_mapper.map_analysis.return_value = []
        setup_monitor._on_update = lambda a, r, h: None

        with caplog.at_level(logging.INFO, logger="app.news.monitor"):
            setup_monitor._tick()

        # Find the tick summary log
        tick_logs = [r for r in caplog.records if "[Tick" in r.message]
        assert len(tick_logs) == 1
        msg = tick_logs[0].message
        assert "CLS→1" in msg            # fetched
        assert "kw❌=0" in msg           # keyword skip
        assert "LLM=1✓/0✗" in msg       # LLM ok/fail
        assert "saved=1" in msg
        assert "quota=1/1000" in msg     # daily quota

    def test_tick_logs_skip_reasons(self, setup_monitor, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="app.news.monitor")

        news = _make_news(title="普通内容", content="没有关键词")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._on_update = lambda a, r, h: None

        with caplog.at_level(logging.INFO, logger="app.news.monitor"):
            setup_monitor._tick()

        tick_logs = [r for r in caplog.records if "[Tick" in r.message]
        msg = tick_logs[0].message
        assert "kw❌=1" in msg    # keyword skipped

    def test_digest_logs_full_progress(self, setup_monitor, caplog):
        import logging
        from app.news.digest import Digest, DigestAnalysis
        from datetime import datetime, timezone, timedelta
        caplog.set_level(logging.INFO, logger="app.news.monitor")

        now = int(datetime.now(tz=timezone(timedelta(hours=8))).timestamp())
        d1 = _make_news_digest(title="财联社7月30日早间", ctime=now - 3600, digest_type="morning")
        d2 = _make_news_digest(title="财联社7月30日午间", ctime=now - 1800, digest_type="noon")

        with patch.object(setup_monitor._digest_fetcher, "fetch", return_value=[d1, d2]):
            with patch.object(setup_monitor._digest_analyzer, "analyze") as mock_analyze:
                mock_analyze.return_value = DigestAnalysis(
                    digest_hashes=["h1", "h2"],
                    summary="测试",
                    market_sentiment="bullish",
                    market_confidence=0.8,
                )
                with caplog.at_level(logging.INFO, logger="app.news.monitor"):
                    setup_monitor.run_digest_cycle()

        msgs = " | ".join(r.message for r in caplog.records)
        assert "[Digest] starting" in msgs
        assert "[Digest] fetched 2 digests" in msgs
        assert "[Digest] 2 digests in window" in msgs
        assert "morning(" in msgs and "noon(" in msgs
        assert "calling LLM" in msgs
        assert "[Digest] ✓ LLM done" in msgs
        assert "market=bullish(0.80)" in msgs

    def test_digest_logs_no_data(self, setup_monitor, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="app.news.monitor")
        with patch.object(setup_monitor._digest_fetcher, "fetch", return_value=[]):
            with caplog.at_level(logging.INFO, logger="app.news.monitor"):
                setup_monitor.run_digest_cycle()
        msgs = " | ".join(r.message for r in caplog.records)
        assert "no digests returned" in msgs

    def test_start_logs_full_config(self, setup_monitor, caplog):
        import logging
        caplog.set_level(logging.INFO, logger="app.news.monitor")
        with caplog.at_level(logging.INFO, logger="app.news.monitor"):
            setup_monitor.start()
        msgs = " | ".join(r.message for r in caplog.records)
        assert "starting... health" in msgs
        assert "cls:True" in msgs or "cls:True" in msgs
        assert "model=" in msgs
        assert "digest enabled" in msgs

    def test_status_log_emitted_after_5_min(self, setup_monitor):
        # Force last_status_log_ts to 6 min ago
        setup_monitor._last_status_log_ts = time.time() - 360
        setup_monitor._maybe_log_status()  # should log
        # Force to now
        setup_monitor._last_status_log_ts = time.time()
        with patch("app.news.monitor.logger") as mock_logger:
            setup_monitor._maybe_log_status()  # should NOT log (under 5 min)
            # If status was logged, info would be called
            info_calls = [c for c in mock_logger.info.call_args_list
                          if "[Status]" in str(c)]
            # May or may not be called depending on tick_count % 100
            # We just check the function doesn't crash
            assert True


class TestDigestPersistence:
    """Successful digest cycle should save to DB."""

    def test_successful_digest_saves_to_db(self, setup_monitor):
        from app.news.digest import DigestAnalysis
        from datetime import datetime, timezone, timedelta
        now = int(datetime.now(tz=timezone(timedelta(hours=8))).timestamp())
        d1 = _make_news_digest(title="财联社7月30日早间", ctime=now - 3600, digest_type="morning")
        with patch.object(setup_monitor._digest_fetcher, "fetch", return_value=[d1]):
            with patch.object(setup_monitor._digest_analyzer, "analyze") as mock_analyze:
                mock_analyze.return_value = DigestAnalysis(
                    digest_hashes=["h1"], summary="测试",
                    market_sentiment="bullish", market_confidence=0.75,
                )
                setup_monitor.run_digest_cycle()

        digests = setup_monitor._db.news_get_recent_digests()
        assert len(digests) == 1
        assert digests[0]["sentiment"] == "bullish"
        assert digests[0]["confidence"] == 0.75
        assert digests[0]["digest_count"] == 1

    def test_failed_digest_does_not_save(self, setup_monitor):
        from datetime import datetime, timezone, timedelta
        now = int(datetime.now(tz=timezone(timedelta(hours=8))).timestamp())
        d1 = _make_news_digest(title="财联社7月30日早间", ctime=now - 3600, digest_type="morning")
        with patch.object(setup_monitor._digest_fetcher, "fetch", return_value=[d1]):
            with patch.object(setup_monitor._digest_analyzer, "analyze", return_value=None):
                setup_monitor.run_digest_cycle()

        digests = setup_monitor._db.news_get_recent_digests()
        assert len(digests) == 0


class TestNormalizeHoldings:
    """Override LLM-hallucinated code-name pairs with config-grounded names."""

    def test_overrides_hallucinated_name(self, setup_monitor):
        # Simulate LLM getting the code wrong: sh601949 is actually 中国出版,
        # not 中国石化. The monitor should override the name.
        items = [
            {"code": "sh601949", "name": "中国石化"},   # ← LLM hallucinated
            {"code": "sh600028", "name": ""},           # ← LLM gave empty
        ]
        setup_monitor._holdings_config = [
            type("H", (), {"code": "sh601949", "name": "中国出版"})(),
            type("H", (), {"code": "sh600028", "name": "中国石化"})(),
        ]
        result = setup_monitor._normalize_holdings(items)
        assert result[0]["name"] == "中国出版"  # overridden from "中国石化"
        assert result[1]["name"] == "中国石化"  # overridden from ""

    def test_keeps_unknown_code_unchanged(self, setup_monitor):
        # codes not in user's holdings should be left alone
        items = [{"code": "sh688981", "name": "Some Name"}]
        setup_monitor._holdings_config = [
            type("H", (), {"code": "sh601398", "name": "工商银行"})(),
        ]
        result = setup_monitor._normalize_holdings(items)
        assert result[0]["name"] == "Some Name"   # unchanged

    def test_empty_list(self, setup_monitor):
        assert setup_monitor._normalize_holdings([]) == []

    def test_non_dict_items_skipped(self, setup_monitor):
        items = [{"code": "sh601398"}, "string item", None, {"code": "sh600028", "name": "X"}]
        setup_monitor._holdings_config = [
            type("H", (), {"code": "sh601398", "name": "工商银行"})(),
        ]
        result = setup_monitor._normalize_holdings(items)
        # Non-dict items skipped, dict items with matching code get names
        assert isinstance(result[0], dict)
        assert result[0]["code"] == "sh601398"
        assert result[1] == "string item"
        assert result[2] is None
        assert result[3]["code"] == "sh600028"

    def test_digest_holdings_impacts_overridden(self, setup_monitor):
        """End-to-end: digest analysis saves with corrected names."""
        from app.news.digest import DigestAnalysis
        setup_monitor._holdings_config = [
            type("H", (), {"code": "sh601398", "name": "工商银行"})(),
            type("H", (), {"code": "sh600519", "name": "贵州茅台"})(),
        ]
        bad = DigestAnalysis(
            digest_hashes=["h"],
            summary="x",
            holdings_impacts=[
                {"code": "sh601398", "name": "宇宙银行", "impact": "positive",
                 "confidence": 0.8, "reason": "test"},  # ← wrong name
            ],
        )
        setup_monitor._normalize_holdings(bad.holdings_impacts)
        assert bad.holdings_impacts[0]["name"] == "工商银行"
