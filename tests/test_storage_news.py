"""Tests for news-related methods on PriceDB."""
import time

from app.storage import PriceDB


def _sample_dict(hash="h1"):
    return {
        "news_hash": hash,
        "summary": "央行降准",
        "sectors": ["银行", "地产"],
        "stocks": [{"code": "sh601398", "name": "工商银行"}],
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
        assert result["stocks"][0].code == "sh601398"
        assert result["stocks"][0].name == "工商银行"

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
            "stocks": ["sz002371", "sh688041"],  # legacy codes-as-strings
            "related": [],
            "direction": "bullish",
            "confidence": 0.9,
            "time_horizon": "intraday",
            "rationale": "中文测试",
        }
        db.news_save_analysis(data)
        result = db.news_get_analysis("h")
        assert result["sectors"] == ["半导体", "集成电路"]
        assert result["stocks"][0].code == "sz002371"
        assert result["stocks"][1].code == "sh688041"
        assert result["stocks"][0].name == ""
        assert result["summary"] == "中文"

class TestEnhancedAnalysis:
    def test_save_and_retrieve_enhanced_fields(self, temp_db_path):
        db = PriceDB(temp_db_path)
        data = {
            "news_hash": "h1",
            "summary": "AI 算力订单爆发",
            "sectors": ["半导体"],
            "stocks": ["sh688981"],
            "direction": "bullish",
            "confidence": 0.9,
            "time_horizon": "intraday",
            "rationale": "下游需求强劲",
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
        db.news_save_analysis(data)
        result = db.news_get_analysis("h1")
        assert result["news_category"] == "order"
        assert result["bottleneck_order_signal"] == "strong"
        assert result["is_kneck"] is True
        assert result["scarcity_pillars"] == ["tech_moat", "single_point"]
        assert result["trend_horizon_years"] == 5
        assert result["industry_certainty"] == "established"
        assert result["narrative_themes"] == ["AI算力", "国产替代"]

    def test_enhanced_defaults_when_table_empty(self, temp_db_path):
        db = PriceDB(temp_db_path)
        data = {
            "news_hash": "h1",
            "summary": "x",
            "direction": "neutral",
            "confidence": 0.3,
        }
        db.news_save_analysis(data)
        result = db.news_get_analysis("h1")
        assert result["news_category"] == "general"
        assert result["is_kneck"] is False
        assert result["scarcity_pillars"] == []

    def test_recent_includes_enhanced_fields(self, temp_db_path):
        db = PriceDB(temp_db_path)
        data = {
            "news_hash": "h1",
            "summary": "x",
            "direction": "bullish",
            "confidence": 0.8,
            "is_kneck": True,
            "news_category": "patent",
        }
        db.news_save_analysis(data)
        results = db.news_get_recent_analyses()
        assert results[0]["is_kneck"] is True
        assert results[0]["news_category"] == "patent"


class TestNewsDigest:
    def test_save_and_retrieve(self, temp_db_path):
        db = PriceDB(temp_db_path)
        d = {
            "analyzed_at": 1785372000.0,
            "date_range": "2026-07-30 ~ 2026-07-30",
            "sentiment": "volatile",
            "confidence": 0.78,
            "summary": "市场波动但有结构性机会",
            "rationale": "综合来看政策面与资金面共同支撑",
            "sector_impacts": [
                {"sector": "半导体/AI算力", "direction": "bullish",
                 "magnitude": "high", "reason": "长鑫科技上市"}
            ],
            "holdings_impacts": [
                {"code": "sh601398", "name": "工商银行", "impact": "neutral",
                 "confidence": 0.5, "reason": "银行业整体稳定"},
            ],
            "key_events": ["央行续作MLF", "长鑫科技上市", "MSCI纳入"],
            "narrative_themes": ["AI算力", "国产替代"],
            "digest_count": 3,
            "digest_hashes": ["h1", "h2", "h3"],
        }
        db.news_save_digest(d)

        result = db.news_get_recent_digests()
        assert len(result) == 1
        r = result[0]
        assert r["sentiment"] == "volatile"
        assert r["confidence"] == 0.78
        assert r["summary"] == "市场波动但有结构性机会"
        assert len(r["sector_impacts"]) == 1
        assert r["sector_impacts"][0]["sector"] == "半导体/AI算力"
        assert len(r["holdings_impacts"]) == 1
        assert r["holdings_impacts"][0]["name"] == "工商银行"
        assert r["narrative_themes"] == ["AI算力", "国产替代"]
        assert r["digest_count"] == 3

    def test_get_recent_digests_ordered_by_time(self, temp_db_path):
        db = PriceDB(temp_db_path)
        db.news_save_digest({"analyzed_at": 100.0, "date_range": "older",
                            "sentiment": "bullish", "confidence": 0.7,
                            "summary": "old", "rationale": "",
                            "sector_impacts": [], "holdings_impacts": [],
                            "key_events": [], "narrative_themes": [],
                            "digest_count": 1, "digest_hashes": []})
        db.news_save_digest({"analyzed_at": 200.0, "date_range": "newer",
                            "sentiment": "bearish", "confidence": 0.8,
                            "summary": "new", "rationale": "",
                            "sector_impacts": [], "holdings_impacts": [],
                            "key_events": [], "narrative_themes": [],
                            "digest_count": 1, "digest_hashes": []})
        result = db.news_get_recent_digests()
        assert len(result) == 2
        assert result[0]["date_range"] == "newer"
        assert result[1]["date_range"] == "older"

    def test_get_recent_digests_limit(self, temp_db_path):
        db = PriceDB(temp_db_path)
        for i in range(5):
            db.news_save_digest({"analyzed_at": float(i), "date_range": f"d{i}",
                                "sentiment": "neutral", "confidence": 0.5,
                                "summary": "", "rationale": "",
                                "sector_impacts": [], "holdings_impacts": [],
                                "key_events": [], "narrative_themes": [],
                                "digest_count": 1, "digest_hashes": []})
        result = db.news_get_recent_digests(limit=3)
        assert len(result) == 3

    def test_save_digest_unicode(self, temp_db_path):
        db = PriceDB(temp_db_path)
        d = {
            "analyzed_at": 100.0,
            "date_range": "中文日期范围",
            "sentiment": "volatile",
            "confidence": 0.7,
            "summary": "中文摘要：央行降准释放流动性",
            "rationale": "中文推理",
            "sector_impacts": [{"sector": "半导体/AI算力"}],
            "holdings_impacts": [{"code": "sh601398", "name": "工商银行"}],
            "key_events": ["事件1", "事件2"],
            "narrative_themes": ["AI算力", "国产替代"],
            "digest_count": 1,
            "digest_hashes": ["h1"],
        }
        db.news_save_digest(d)
        r = db.news_get_recent_digests()[0]
        assert r["date_range"] == "中文日期范围"
        assert r["summary"] == "中文摘要：央行降准释放流动性"
        assert r["sector_impacts"][0]["sector"] == "半导体/AI算力"


class TestDigestSchemaVersion:
    """Migration: drop old-schema digests automatically on save/read."""

    def test_save_digest_with_default_version(self, temp_db_path):
        from app.storage import PriceDB
        db = PriceDB(temp_db_path)
        db.news_save_digest({"analyzed_at": 100.0, "date_range": "x",
                            "sentiment": "bullish", "confidence": 0.5,
                            "summary": "s", "rationale": "r",
                            "sector_impacts": [], "holdings_impacts": [],
                            "key_events": [], "narrative_themes": [],
                            "digest_count": 1, "digest_hashes": []})
        digests = db.news_get_recent_digests()
        assert len(digests) == 1
        assert digests[0]["schema_version"] == 1

    def test_save_digest_with_explicit_version(self, temp_db_path):
        from app.storage import PriceDB
        db = PriceDB(temp_db_path)
        db.news_save_digest({"analyzed_at": 100.0, "date_range": "x",
                            "sentiment": "bullish", "confidence": 0.5,
                            "summary": "s", "rationale": "r",
                            "sector_impacts": [], "holdings_impacts": [],
                            "key_events": [], "narrative_themes": [],
                            "digest_count": 1, "digest_hashes": []},
                           schema_version=3)
        digests = db.news_get_recent_digests()
        assert digests[0]["schema_version"] == 3

    def test_migrate_drops_old_version(self, temp_db_path):
        import sqlite3
        from app.storage import PriceDB
        db = PriceDB(temp_db_path)
        # Insert 2 old-version rows manually (bypass save to set schema_version=1)
        conn = sqlite3.connect(temp_db_path)
        conn.execute(
            "INSERT INTO news_digests (analyzed_at, schema_version, date_range, sentiment, confidence, summary, rationale, sector_impacts, holdings_impacts, key_events, narrative_themes, digest_count, digest_hashes) "
            "VALUES (100.0, 1, 'old1', 'bullish', 0.5, 'a', 'b', '[]', '[]', '[]', '[]', 1, '[]')"
        )
        conn.execute(
            "INSERT INTO news_digests (analyzed_at, schema_version, date_range, sentiment, confidence, summary, rationale, sector_impacts, holdings_impacts, key_events, narrative_themes, digest_count, digest_hashes) "
            "VALUES (200.0, 2, 'new1', 'bullish', 0.5, 'a', 'b', '[]', '[]', '[]', '[]', 1, '[]')"
        )
        conn.commit()
        conn.close()

        # Before migration
        digests = db.news_get_recent_digests()
        assert len(digests) == 2

        # Run migration to v3
        dropped = db.news_migrate_digests(3)
        assert dropped == 2  # both old ones dropped (1 and 2 < 3)

        digests = db.news_get_recent_digests()
        assert len(digests) == 0  # all dropped

    def test_migrate_idempotent(self, temp_db_path):
        from app.storage import PriceDB
        db = PriceDB(temp_db_path)
        db.news_save_digest({"analyzed_at": 100.0, "date_range": "x",
                            "sentiment": "bullish", "confidence": 0.5,
                            "summary": "s", "rationale": "r",
                            "sector_impacts": [], "holdings_impacts": [],
                            "key_events": [], "narrative_themes": [],
                            "digest_count": 1, "digest_hashes": []})
        # Run migration twice, second should be no-op
        assert db.news_migrate_digests(1) == 0
        assert db.news_migrate_digests(1) == 0
