import logging
import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class StockQuote:
    code: str
    name: str
    current_price: float
    yesterday_close: float
    change_pct: float
    change_amount: float
    volume: float = 0.0


class StockFetcher:
    def __init__(self, api_template: str = "http://qt.gtimg.cn/q={codes}"):
        self._api_template = api_template
        self._cache: Dict[str, StockQuote] = {}
        self._lock = threading.Lock()

    def fetch(self, codes: List[str]) -> List[StockQuote]:
        if not codes:
            return []

        codes_str = ",".join(codes)
        url = self._api_template.format(codes=codes_str)

        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            text = response.text
        except requests.RequestException as e:
            logger.warning(f"Network error fetching stock data: {e}")
            with self._lock:
                return [self._cache[code] for code in codes if code in self._cache]

        quotes = self._parse_response(text)

        with self._lock:
            for quote in quotes:
                self._cache[quote.code] = quote

        return quotes

    def _parse_response(self, text: str) -> List[StockQuote]:
        quotes: List[StockQuote] = []
        if not text:
            return quotes

        # Split by semicolon to get individual stock entries
        entries = text.strip().split(";")

        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue

            # Match pattern: v_sh600519="1~贵州茅台~600519~1322.15~1253.00~..."
            match = re.match(r'v_(sh\d+|sz\d+)="(.*)"', entry)
            if not match:
                continue

            code = match.group(1)
            data_str = match.group(2)
            fields = data_str.split("~")

            if len(fields) < 5:
                logger.warning(f"Insufficient fields for {code}: {len(fields)}")
                continue

            try:
                name = fields[1] if len(fields) > 1 else code
                current_price = float(fields[3]) if fields[3] else 0.0
                yesterday_close = float(fields[4]) if fields[4] else 0.0

                # Try to get change_pct from index 32
                change_pct = 0.0
                if len(fields) > 32 and fields[32]:
                    try:
                        change_pct = float(fields[32])
                    except ValueError:
                        pass

                # Fallback calculation if index 32 fails or is empty
                if change_pct == 0.0 and yesterday_close > 0:
                    change_pct = (current_price - yesterday_close) / yesterday_close * 100

                change_amount = current_price - yesterday_close

                # Parse volume from index 6
                volume = 0.0
                if len(fields) > 6 and fields[6]:
                    try:
                        volume = float(fields[6])
                    except ValueError:
                        pass

                quote = StockQuote(
                    code=code,
                    name=name,
                    current_price=current_price,
                    yesterday_close=yesterday_close,
                    change_pct=round(change_pct, 2),
                    change_amount=round(change_amount, 2),
                    volume=volume,
                )
                quotes.append(quote)
            except (ValueError, IndexError) as e:
                logger.warning(f"Error parsing data for {code}: {e}")
                continue

        return quotes
