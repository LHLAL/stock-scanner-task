"""SQLite storage for price history data."""
import logging
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

from app.multi_fetcher import StockQuote

logger = logging.getLogger(__name__)


class PriceDB:
    def __init__(self, db_path: str = "price_history.db"):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    code TEXT,
                    timestamp REAL,
                    price REAL,
                    volume REAL,
                    change_pct REAL,
                    PRIMARY KEY (code, timestamp)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_code ON price_history(code)")

            self._migrate_news_cache(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS news_cache (
                    hash TEXT PRIMARY KEY,
                    title TEXT,
                    content TEXT,
                    ctime REAL,
                    first_seen REAL,
                    last_seen REAL,
                    analyzed INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS news_analysis (
                    news_hash TEXT PRIMARY KEY,
                    summary TEXT,
                    sectors TEXT,
                    stocks TEXT,
                    direction TEXT,
                    confidence REAL,
                    time_horizon TEXT,
                    rationale TEXT,
                    analyzed_at REAL,
                    notified INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS news_analysis_enhanced (
                    news_hash TEXT PRIMARY KEY,
                    news_category TEXT,
                    bottleneck_order_signal TEXT,
                    bottleneck_capacity_signal TEXT,
                    bottleneck_margin_signal TEXT,
                    is_kneck INTEGER,
                    scarcity_pillars TEXT,
                    trend_horizon_years INTEGER,
                    industry_certainty TEXT,
                    narrative_themes TEXT
                )
            """)
            conn.commit()
            conn.close()

    @staticmethod
    def _migrate_news_cache(conn: sqlite3.Connection) -> None:
        """Drop legacy news_cache schema (from earlier dev) if incompatible."""
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(news_cache)").fetchall()
        }
        if cols and "last_seen" not in cols:
            conn.execute("DROP TABLE news_cache")

    @staticmethod
    def _migrate_news_analysis_enhanced(conn: sqlite3.Connection) -> None:
        """No-op for fresh schema; placeholder for future migrations."""
        return

    def save_quotes(self, quotes: List[StockQuote]) -> None:
        """Save current quotes to history."""
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            for q in quotes:
                conn.execute(
                    "INSERT OR REPLACE INTO price_history (code, timestamp, price, volume, change_pct) VALUES (?, ?, ?, ?, ?)",
                    (q.code, now, q.current_price, 0, q.change_pct)
                )
            conn.commit()
            conn.close()

    def get_recent(self, code: str, n: int = 20) -> List[Tuple[float, float]]:
        """Get last N (price, timestamp) records for a stock."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT price, timestamp FROM price_history WHERE code = ? ORDER BY timestamp DESC LIMIT ?",
                (code, n)
            ).fetchall()
            conn.close()
            return [(row[0], row[1]) for row in rows]

    def calc_sma(self, code: str, period: int) -> Optional[float]:
        """Calculate Simple Moving Average over last N periods."""
        prices = []
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT price FROM price_history WHERE code = ? ORDER BY timestamp DESC LIMIT ?",
                (code, period)
            ).fetchall()
            conn.close()
            prices = [row[0] for row in rows]
        if len(prices) < period:
            return None
        return sum(prices) / len(prices)

    def cleanup(self, days: int = 7) -> None:
        """Remove data older than specified days."""
        cutoff = time.time() - days * 86400
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute("DELETE FROM price_history WHERE timestamp < ?", (cutoff,))
            conn.execute("DELETE FROM news_cache WHERE last_seen < ?", (cutoff,))
            conn.execute("DELETE FROM news_analysis WHERE analyzed_at < ?", (cutoff,))
            conn.commit()
            conn.close()

    def news_seen(self, news_hash: str, title: str, content: str, ctime: int) -> bool:
        """Return True if news was seen in last 24h (cache hit), False if new."""
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT last_seen FROM news_cache WHERE hash = ?", (news_hash,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE news_cache SET last_seen = ? WHERE hash = ?",
                    (now, news_hash),
                )
                conn.commit()
                conn.close()
                return True
            conn.execute(
                "INSERT INTO news_cache (hash, title, content, ctime, first_seen, last_seen, analyzed) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (news_hash, title, content, float(ctime), now, now),
            )
            conn.commit()
            conn.close()
            return False

    def news_save_analysis(self, analysis_dict: dict) -> None:
        """Persist a NewsAnalysis (dict form) for caching/dedup."""
        import json
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT OR REPLACE INTO news_analysis "
                "(news_hash, summary, sectors, stocks, direction, confidence, time_horizon, rationale, analyzed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis_dict["news_hash"],
                    analysis_dict.get("summary", ""),
                    json.dumps(analysis_dict.get("sectors", []), ensure_ascii=False),
                    json.dumps(analysis_dict.get("stocks", []), ensure_ascii=False),
                    analysis_dict.get("direction", "neutral"),
                    float(analysis_dict.get("confidence", 0.0)),
                    analysis_dict.get("time_horizon", "intraday"),
                    analysis_dict.get("rationale", ""),
                    now,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO news_analysis_enhanced "
                "(news_hash, news_category, bottleneck_order_signal, bottleneck_capacity_signal, "
                "bottleneck_margin_signal, is_kneck, scarcity_pillars, trend_horizon_years, "
                "industry_certainty, narrative_themes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis_dict["news_hash"],
                    analysis_dict.get("news_category", "general"),
                    analysis_dict.get("bottleneck_order_signal", "none"),
                    analysis_dict.get("bottleneck_capacity_signal", "none"),
                    analysis_dict.get("bottleneck_margin_signal", "unknown"),
                    1 if analysis_dict.get("is_kneck", False) else 0,
                    json.dumps(analysis_dict.get("scarcity_pillars", []), ensure_ascii=False),
                    int(analysis_dict.get("trend_horizon_years", 1)),
                    analysis_dict.get("industry_certainty", "speculative"),
                    json.dumps(analysis_dict.get("narrative_themes", []), ensure_ascii=False),
                ),
            )
            conn.execute(
                "UPDATE news_cache SET analyzed = 1 WHERE hash = ?",
                (analysis_dict["news_hash"],),
            )
            conn.commit()
            conn.close()

    def news_get_analysis(self, news_hash: str, ttl_hours: int = 24) -> Optional[dict]:
        """Fetch cached analysis if still fresh."""
        import json
        cutoff = time.time() - ttl_hours * 3600
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            base = conn.execute(
                "SELECT summary, sectors, stocks, direction, confidence, time_horizon, rationale, analyzed_at "
                "FROM news_analysis WHERE news_hash = ? AND analyzed_at >= ?",
                (news_hash, cutoff),
            ).fetchone()
            enh = conn.execute(
                "SELECT news_category, bottleneck_order_signal, bottleneck_capacity_signal, "
                "bottleneck_margin_signal, is_kneck, scarcity_pillars, trend_horizon_years, "
                "industry_certainty, narrative_themes "
                "FROM news_analysis_enhanced WHERE news_hash = ?",
                (news_hash,),
            ).fetchone()
            conn.close()
        if not base:
            return None
        result = {
            "news_hash": news_hash,
            "summary": base[0] or "",
            "sectors": json.loads(base[1]) if base[1] else [],
            "stocks": json.loads(base[2]) if base[2] else [],
            "direction": base[3] or "neutral",
            "confidence": float(base[4] or 0.0),
            "time_horizon": base[5] or "intraday",
            "rationale": base[6] or "",
            "news_category": "general",
            "bottleneck_order_signal": "none",
            "bottleneck_capacity_signal": "none",
            "bottleneck_margin_signal": "unknown",
            "is_kneck": False,
            "scarcity_pillars": [],
            "trend_horizon_years": 1,
            "industry_certainty": "speculative",
            "narrative_themes": [],
        }
        if enh:
            result.update({
                "news_category": enh[0] or "general",
                "bottleneck_order_signal": enh[1] or "none",
                "bottleneck_capacity_signal": enh[2] or "none",
                "bottleneck_margin_signal": enh[3] or "unknown",
                "is_kneck": bool(enh[4]),
                "scarcity_pillars": json.loads(enh[5]) if enh[5] else [],
                "trend_horizon_years": int(enh[6] or 1),
                "industry_certainty": enh[7] or "speculative",
                "narrative_themes": json.loads(enh[8]) if enh[8] else [],
            })
        return result

    def news_mark_notified(self, news_hash: str) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "UPDATE news_analysis SET notified = 1 WHERE news_hash = ?",
                (news_hash,),
            )
            conn.commit()
            conn.close()

    def news_get_recent_analyses(self, limit: int = 20) -> List[dict]:
        """返回最近的分析结果（按 analyzed_at 倒序）."""
        import json
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            rows = conn.execute(
                "SELECT news_hash, summary, sectors, stocks, direction, confidence, time_horizon, rationale, analyzed_at "
                "FROM news_analysis ORDER BY analyzed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            enh_rows = conn.execute(
                "SELECT news_hash, news_category, bottleneck_order_signal, bottleneck_capacity_signal, "
                "bottleneck_margin_signal, is_kneck, scarcity_pillars, trend_horizon_years, "
                "industry_certainty, narrative_themes FROM news_analysis_enhanced"
            ).fetchall()
            conn.close()
        enh_map = {r[0]: r for r in enh_rows}
        result: List[dict] = []
        for row in rows:
            base = {
                "news_hash": row[0],
                "summary": row[1] or "",
                "sectors": json.loads(row[2]) if row[2] else [],
                "stocks": json.loads(row[3]) if row[3] else [],
                "direction": row[4] or "neutral",
                "confidence": float(row[5] or 0.0),
                "time_horizon": row[6] or "intraday",
                "rationale": row[7] or "",
                "news_category": "general",
                "bottleneck_order_signal": "none",
                "bottleneck_capacity_signal": "none",
                "bottleneck_margin_signal": "unknown",
                "is_kneck": False,
                "scarcity_pillars": [],
                "trend_horizon_years": 1,
                "industry_certainty": "speculative",
                "narrative_themes": [],
            }
            enh = enh_map.get(row[0])
            if enh:
                base.update({
                    "news_category": enh[1] or "general",
                    "bottleneck_order_signal": enh[2] or "none",
                    "bottleneck_capacity_signal": enh[3] or "none",
                    "bottleneck_margin_signal": enh[4] or "unknown",
                    "is_kneck": bool(enh[5]),
                    "scarcity_pillars": json.loads(enh[6]) if enh[6] else [],
                    "trend_horizon_years": int(enh[7] or 1),
                    "industry_certainty": enh[8] or "speculative",
                    "narrative_themes": json.loads(enh[9]) if enh[9] else [],
                })
            result.append(base)
        return result
