"""Sector → representative stocks mapping.

v1: 手工维护的 docs/sector_dict.json（约 70 个主流板块，230+ 代表股）。
未来可扩展为 akshare/东方财富自动拉取（这些源当前不稳定）。
"""
import json
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


CACHE_FILE = Path(__file__).parent.parent.parent / "docs" / "sector_dict.json"
FUZZY_THRESHOLD = 0.75


class SectorMapper:
    def __init__(self, cache_path: Optional[Path] = None):
        self._cache_path = cache_path or CACHE_FILE
        self._sector_to_stocks: Dict[str, List[str]] = {}
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
                self._sector_to_stocks[sector] = list(stocks)
                for code in stocks:
                    self._stock_to_sectors.setdefault(code, set()).add(sector)

        logger.info(
            f"[SectorMapper] loaded {len(self._sector_to_stocks)} sectors, "
            f"{len(self._stock_to_sectors)} stocks"
        )

    def match_sector(self, sector_name: str) -> List[str]:
        """Match a sector name (from LLM) to local stocks via fuzzy match."""
        if not sector_name:
            return []
        sector_name = sector_name.strip()

        if sector_name in self._sector_to_stocks:
            return list(self._sector_to_stocks[sector_name])

        for key in self._sector_to_stocks:
            if sector_name in key or key in sector_name:
                return list(self._sector_to_stocks[key])

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
            return list(self._sector_to_stocks[best_key])

        return []

    def map_analysis(self, sectors: List[str], llm_stocks: List[str]) -> List[str]:
        """Return deduplicated list of stock codes related to sectors + LLM stocks."""
        codes: Set[str] = set()
        for sector in sectors:
            for code in self.match_sector(sector):
                codes.add(code)
        for stock in llm_stocks:
            stock = stock.strip().lower()
            if stock.startswith(("sh", "sz")) and len(stock) == 8:
                codes.add(stock)
        return sorted(codes)

    def get_stock_sectors(self, code: str) -> List[str]:
        """Reverse lookup: which sectors does this stock belong to."""
        return sorted(self._stock_to_sectors.get(code, set()))