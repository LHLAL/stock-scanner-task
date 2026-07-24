"""SQLite storage for price history data."""
import logging
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

from app.fetcher import StockQuote

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
            conn.commit()
            conn.close()

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
            conn.commit()
            conn.close()
