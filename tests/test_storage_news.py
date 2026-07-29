"""Tests for news-related methods on PriceDB."""
import time

from app.storage import PriceDB


def _sample_dict(hash="h1"):
    return {
        "news_hash": hash,
        "summary": "央行降准",
        "sectors": ["银行", "地产"],
        "stocks": ["sh601398"],
        "direction": "bullish",
        "confidence": 0.85,
        "time_horizon": "next_day",
        "rationale": "释放流动性",
    }


class TestNewsCache:
    def test_first_seen_returns_false(self, temp_db_path):
        db = PriceDB(temp_db_path)
        assert db.news_seen("h1", "央行降准", "降准0.5%", 1785234699) is False

    def test_second_seen_returns_true(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_seen("h1", "央行降准", "降准0.5%", 1785234699)
        assert db.news_seen("h1", "央行降准", "降准0.5%", 1785234699) is True

    def test_different_hash_returns_false(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_seen("h1", "title", "content", 100)
        assert db.news_seen("h2", "title", "content", 100) is False


class TestNewsAnalysis:
    def test_save_and_retrieve(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_save_analysis(_sample_dict())
        result = db.news_get_analysis("h1")
        assert result is not None
        assert result["summary"] == "央行降准"
        assert result["direction"] == "bullish"
        assert result["confidence"] == 0.85
        assert result["sectors"] == ["银行", "地产"]
        assert result["stocks"] == ["sh601398"]

    def test_get_returns_none_when_missing(self, temp_db_path):
        db = PriceDB(temp_db_path)
        assert db.news_get_analysis("nonexistent") is None

    def test_save_updates_cache_analyzed_flag(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_seen("h1", "title", "content", 100)
        db.news_save_analysis(_sample_dict())
        # The cache should now have analyzed=1
        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT analyzed FROM news_cache WHERE hash = ?", ("h1",)
        ).fetchone()[0]
        conn.close()
        assert flag == 1

    def test_save_then_overwrite(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_save_analysis(_sample_dict("h1"))
        db.news_save_analysis({**_sample_dict("h1"), "confidence": 0.5})
        result = db.news_get_analysis("h1")
        assert result["confidence"] == 0.5

    def test_mark_notified(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_save_analysis(_sample_dict("h1"))
        db.news_mark_notified("h1")
        # Should not raise; flag updated
        import sqlite3
        conn = sqlite3.connect(temp_db_path)
        flag = conn.execute(
            "SELECT notified FROM news_analysis WHERE news_hash = ?", ("h1",)
        ).fetchone()[0]
        conn.close()
        assert flag == 1

    def test_get_recent_analyses_ordered_by_time(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_save_analysis(_sample_dict("h1"))
        time.sleep(0.01)
        db.news_save_analysis({**_sample_dict("h2"), "summary": "second"})
        results = db.news_get_recent_analyses(limit=10)
        assert len(results) == 2
        assert results[0]["news_hash"] == "h2"
        assert results[1]["news_hash"] == "h1"

    def test_get_recent_respects_limit(self, temp_db_path):
        db = PriceDB(temp_db_path)
        for i in range(5):
            db.news_save_analysis(_sample_dict(f"h{i}"))
        results = db.news_get_recent_analyses(limit=2)
        assert len(results) == 2

    def test_ttl_excludes_old_analyses(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_save_analysis(_sample_dict("h1"))
        # Pass tiny TTL → should be considered expired
        assert db.news_get_analysis("h1", ttl_hours=0) is None

    def test_stocks_sectors_round_trip_unicode(self, temp_db_path):
        db = PriceDB(temp_db_path)
        data = {
            "news_hash": "h",
            "summary": "中文",
            "sectors": ["半导体", "集成电路"],
            "stocks": ["中芯国际", "海光信息"],
            "direction": "bullish",
            "confidence": 0.9,
            "time_horizon": "intraday",
            "rationale": "中文测试",
        }
        db.news_save_analysis(data)
        result = db.news_get_analysis("h")
        assert result["sectors"] == ["半导体", "集成电路"]
        assert result["stocks"] == ["中芯国际", "海光信息"]
        assert result["summary"] == "中文"