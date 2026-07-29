"""Sector → representative stocks mapping.

Each entry in docs/sector_dict.json can be either:
  - legacy: "sh601398" (code only)
  - enriched: {"code": "sh601398", "name": "工商银行"}

Both are normalized into Stock objects.
"""
import json
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Union

from app.news.models import Stock


CACHE_FILE = Path(__file__).parent.parent.parent / "docs" / "sector_dict.json"
FUZZY_THRESHOLD = 0.75


def _normalize_stock(entry) -> Optional[Stock]:
    if isinstance(entry, str):
        return Stock(code=entry, name="") if entry else None
    if isinstance(entry, dict):
        code = entry.get("code", "")
        name = entry.get("name", "")
        if code:
            return Stock(code=code, name=name or "")
    return None


class SectorMapper:
    def __init__(self, cache_path: Optional[Path] = None):
        self._cache_path = cache_path or CACHE_FILE
        self._sector_to_stocks: Dict[str, List[Stock]] = {}
        self._stock_to_sectors: Dict[str, Set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self._cache_path.exists():
            logger.warning(f"[SectorMapper] cache not found: {self._cache_path}")
            return
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"[SectorMapper] load failed: {e}")
            return

        data.pop("_meta", None)
        self._sector_to_stocks = {}
        self._stock_to_sectors = {}
        for category, sectors in data.items():
            if not isinstance(sectors, dict):
                continue
            for sector, stocks in sectors.items():
                if not isinstance(stocks, list):
                    continue
                normalized = []
                for entry in stocks:
                    stock = _normalize_stock(entry)
                    if stock:
                        normalized.append(stock)
                self._sector_to_stocks[sector] = normalized
                for stock in normalized:
                    self._stock_to_sectors.setdefault(stock.code, set()).add(sector)

        logger.info(
            f"[SectorMapper] loaded {len(self._sector_to_stocks)} sectors, "
            f"{len(self._stock_to_sectors)} stocks"
        )

    def match_sector(self, sector_name: str) -> Sequence[Stock]:
        if not sector_name:
            return []
        sector_name = sector_name.strip()

        result = self._sector_to_stocks.get(sector_name)
        if result is not None:
            return result

        for key in self._sector_to_stocks:
            if sector_name in key or key in sector_name:
                return self._sector_to_stocks[key]

        best_key: Optional[str] = None
        best_score = 0.0
        for key in self._sector_to_stocks:
            score = SequenceMatcher(None, sector_name, key).ratio()
            if score > best_score:
                best_score = score
                best_key = key
        if best_key and best_score >= FUZZY_THRESHOLD:
            logger.debug(
                f"[SectorMapper] fuzzy match: '{sector_name}' → '{best_key}' "
                f"(score={best_score:.2f})"
            )
            return self._sector_to_stocks[best_key]

        return []

    def map_analysis(
        self,
        sectors: Sequence[str],
        llm_stocks: Sequence[Union[str, Stock]],
    ) -> List[Stock]:
        """Deduplicated list of Stock objects from sectors + LLM stocks."""
        by_code: Dict[str, Stock] = {}
        for sector in sectors:
            for stock in self.match_sector(sector):
                if stock.code:
                    by_code[stock.code] = stock

        for stock in llm_stocks:
            if isinstance(stock, Stock):
                code = stock.code.strip().lower()
                if not code.startswith(("sh", "sz")) or len(code) != 8:
                    continue
                existing = by_code.get(code)
                if existing is None or not existing.name:
                    if stock.name:
                        by_code[code] = stock
                    else:
                        by_code.setdefault(code, stock)
            else:
                code = str(stock).strip().lower()
                if code.startswith(("sh", "sz")) and len(code) == 8:
                    by_code.setdefault(code, Stock(code=code, name=""))

        return [by_code[c] for c in sorted(by_code)]

    def get_stock_sectors(self, code: str) -> List[str]:
        """Reverse lookup: which sectors does this stock belong to."""
        return sorted(self._stock_to_sectors.get(code, set()))


import logging  # noqa: E402

logger = logging.getLogger(__name__)