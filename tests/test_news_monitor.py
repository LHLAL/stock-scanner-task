"""Tests for app.news.monitor: NewsMonitor._tick pipeline (mocked fetcher/analyzer)."""
from unittest.mock import MagicMock, patch

import pytest

from app.config import ClsConfig, LlmConfig, NewsConfig, NewsFilterConfig
from app.news.models import NewsAnalysis, RawNews
from app.news.monitor import NewsMonitor
from app.storage import PriceDB


def _make_news(hash="h1", title="央行降准", content="降准0.5%"):
    return RawNews(id=1, title=title, content=content, ctime=1785234699, hash=hash)


def _make_analysis(hash="h1", sectors=None, stocks=None, direction="bullish", confidence=0.85):
    return NewsAnalysis(
        news_hash=hash,
        summary="降准利好",
        sectors=sectors or ["银行"],
        stocks=stocks or ["sh601398"],
        direction=direction,
        confidence=confidence,
    )


def _make_news_config(**overrides):
    cfg = NewsConfig(
        enabled=True,
        cls=ClsConfig(sign=None, cookie=None,
                     poll_interval_seconds=30,
                     off_hours_poll_interval_seconds=300),
        filter=NewsFilterConfig(
            keyword_threshold=0.3,
            min_confidence_for_notify=0.7,
            min_confidence_for_holdings_alert=0.5,
        ),
        llm=LlmConfig(model="test:model", host="http://localhost:11434",
                      api_key=None, max_per_minute=10,
                      cache_ttl_hours=24, request_timeout_seconds=30),
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
        setup_monitor._sector_mapper.map_analysis.return_value = ["sh601398"]

        processed = []
        setup_monitor._on_update = lambda a, r, h: processed.append((a, r, h))
        setup_monitor._tick()

        assert len(processed) == 1
        analysis, related, hits = processed[0]
        assert analysis.confidence == 0.85
        assert "sh601398" in related
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
        setup_monitor._sector_mapper.map_analysis.return_value = ["sh601398"]

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
        setup_monitor._sector_mapper.map_analysis.return_value = ["sh999999"]  # not in holdings

        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("low-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0

    def test_notifies_when_holdings_hit_with_low_confidence(self, setup_monitor, temp_db_path):
        # threshold min_confidence_for_holdings_alert = 0.5
        news = _make_news(hash="hit-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = _make_analysis(
            hash="hit-test", confidence=0.55, direction="bullish"
        )
        setup_monitor._sector_mapper.map_analysis.return_value = ["sh601398"]  # in holdings

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

class TestBottleneckNotify:
    def test_notifies_when_is_kneck_no_high_confidence(self, setup_monitor, temp_db_path):
        news = _make_news(hash="kneck-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        # Confidence below 0.7 but is_kneck=True → should still notify
        setup_monitor._analyzer.analyze.return_value = _make_analysis(
            hash="kneck-test", confidence=0.6, direction="bullish",
        )._replace(is_kneck=True) if False else NewsAnalysis(
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
        assert flag == 1

    def test_notifies_when_strong_order(self, setup_monitor, temp_db_path):
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
        assert flag == 1

    def test_does_not_notify_neutral_no_signals(self, setup_monitor, temp_db_path):
        news = _make_news(hash="boring-test")
        setup_monitor._fetcher.fetch.return_value = [news]
        setup_monitor._analyzer.analyze.return_value = NewsAnalysis(
            news_hash="boring-test", summary="普通", direction="neutral",
            confidence=0.5,
        )
        setup_monitor._sector_mapper.map_analysis.return_value = ["sh999999"]
        setup_monitor._on_update = lambda a, r, h: None
        setup_monitor._tick()

        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("boring-test",)
        ).fetchone()[0]
        conn.close()
        assert flag == 0
