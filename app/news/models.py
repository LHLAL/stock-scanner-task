"""Data models for news intelligence."""
import time
from dataclasses import dataclass, field
from typing import List


@dataclass
class RawNews:
    """Single news item from CLS, pre-LLM."""

    id: int
    title: str
    content: str
    ctime: int                       # unix timestamp from CLS
    type: str = ""                   # 电报解读 / 头条 / etc.
    url: str = ""
    hash: str = ""                   # sha256 of title+content, for dedup

    @property
    def published_at(self) -> float:
        return float(self.ctime)


@dataclass
class NewsAnalysis:
    """LLM-analyzed news with sectors/stocks/prediction."""

    news_hash: str
    summary: str
    sectors: List[str] = field(default_factory=list)
    stocks: List[str] = field(default_factory=list)
    direction: str = "neutral"       # bullish / bearish / neutral
    confidence: float = 0.0
    time_horizon: str = "intraday"
    rationale: str = ""
    analyzed_at: float = field(default_factory=time.time)

    @classmethod
    def from_json(cls, news_hash: str, raw: "str | dict") -> "NewsAnalysis":
        """Parse Ollama JSON response into NewsAnalysis (lenient)."""
        import json
        data = json.loads(raw) if isinstance(raw, str) else raw
        return cls(
            news_hash=news_hash,
            summary=str(data.get("summary", ""))[:80],
            sectors=[str(s)[:20] for s in data.get("sectors", [])][:6],
            stocks=[str(s)[:20] for s in data.get("stocks", [])][:10],
            direction=str(data.get("direction", "neutral")).lower(),
            confidence=_clip(float(data.get("confidence", 0.0))),
            time_horizon=str(data.get("time_horizon", "intraday")),
            rationale=str(data.get("rationale", ""))[:200],
        )

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.7 and self.direction in ("bullish", "bearish")

    @property
    def emoji(self) -> str:
        return {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(self.direction, "⚪")

    @property
    def direction_label(self) -> str:
        return {"bullish": "↗ 利好", "bearish": "↘ 利空", "neutral": "→ 中性"}.get(
            self.direction, "→ 中性"
        )


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))