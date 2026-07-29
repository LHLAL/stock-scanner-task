"""Data models for news intelligence."""
import time
from dataclasses import dataclass, field
from typing import List


VALID_NEWS_CATEGORIES = {
    "policy", "order", "capacity", "financial",
    "patent", "supply_disruption", "general",
}
VALID_BOTTLENECK_SIGNALS = {
    "order": {"none", "mentioned", "strong"},
    "capacity": {"none", "expansion", "utilization_high", "inventory_warning"},
    "margin": {"unknown", "rising", "stable", "declining"},
}
VALID_SCARCITY_PILLARS = {"tech_moat", "single_point", "certification", "long_cycle"}
VALID_CERTAINTY = {"speculative", "emerging", "established", "dominant"}


@dataclass
class Stock:
    """A-share stock with both code and display name."""

    code: str            # "sh601398"
    name: str = ""       # "工商银行"

    def display(self) -> str:
        """Short form for menu items."""
        if self.name and self.code:
            return f"{self.name}({self.code})"
        return self.name or self.code

    def label(self) -> str:
        """Compact form for notifications."""
        return self.name if self.name else self.code


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


def _parse_stocks(raw_list) -> List[Stock]:
    """Parse LLM stocks field. Supports new [{code, name}, ...] and legacy ['code', ...]."""
    if not isinstance(raw_list, list):
        return []
    out: List[Stock] = []
    for item in raw_list:
        if isinstance(item, dict):
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
            if code or name:
                out.append(Stock(code=code, name=name))
        elif isinstance(item, str):
            code = item.strip().lower()
            if code:
                out.append(Stock(code=code, name=""))
    return out[:10]


@dataclass
class NewsAnalysis:
    """LLM-analyzed news with sectors/stocks/prediction + bottleneck thesis."""

    news_hash: str
    summary: str
    sectors: List[str] = field(default_factory=list)
    stocks: List[Stock] = field(default_factory=list)
    direction: str = "neutral"       # bullish / bearish / neutral
    confidence: float = 0.0
    time_horizon: str = "intraday"
    rationale: str = ""
    analyzed_at: float = field(default_factory=time.time)

    news_category: str = "general"   # policy/order/capacity/financial/patent/supply_disruption/general

    bottleneck_order_signal: str = "none"      # none/mentioned/strong
    bottleneck_capacity_signal: str = "none"   # none/expansion/utilization_high/inventory_warning
    bottleneck_margin_signal: str = "unknown"   # unknown/rising/stable/declining
    is_kneck: bool = False
    scarcity_pillars: List[str] = field(default_factory=list)

    trend_horizon_years: int = 1
    industry_certainty: str = "speculative"
    narrative_themes: List[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, news_hash: str, raw: "str | dict") -> "NewsAnalysis":
        """Parse Ollama JSON response into NewsAnalysis (lenient)."""
        import json
        data = json.loads(raw) if isinstance(raw, str) else raw
        order = str(data.get("bottleneck_order_signal", "none"))
        capacity = str(data.get("bottleneck_capacity_signal", "none"))
        margin = str(data.get("bottleneck_margin_signal", "unknown"))
        category = str(data.get("news_category", "general"))
        pillars = [str(p) for p in data.get("scarcity_pillars", []) if p in VALID_SCARCITY_PILLARS]
        themes = [str(t)[:20] for t in data.get("narrative_themes", [])][:5]
        certainty = str(data.get("industry_certainty", "speculative"))
        stocks = _parse_stocks(data.get("stocks", []))
        return cls(
            news_hash=news_hash,
            summary=str(data.get("summary", ""))[:80],
            sectors=[str(s)[:20] for s in data.get("sectors", [])][:6],
            stocks=stocks,
            direction=str(data.get("direction", "neutral")).lower(),
            confidence=_clip(float(data.get("confidence", 0.0))),
            time_horizon=str(data.get("time_horizon", "intraday")),
            rationale=str(data.get("rationale", ""))[:200],
            news_category=category if category in VALID_NEWS_CATEGORIES else "general",
            bottleneck_order_signal=order if order in VALID_BOTTLENECK_SIGNALS["order"] else "none",
            bottleneck_capacity_signal=capacity if capacity in VALID_BOTTLENECK_SIGNALS["capacity"] else "none",
            bottleneck_margin_signal=margin if margin in VALID_BOTTLENECK_SIGNALS["margin"] else "unknown",
            is_kneck=bool(data.get("is_kneck", False)),
            scarcity_pillars=pillars,
            trend_horizon_years=max(1, min(10, int(data.get("trend_horizon_years", 1)))),
            industry_certainty=certainty if certainty in VALID_CERTAINTY else "speculative",
            narrative_themes=themes,
        )

    @property
    def stock_codes(self) -> List[str]:
        """Convenience: just the codes from stocks."""
        return [s.code for s in self.stocks if s.code]

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.7 and self.direction in ("bullish", "bearish")

    @property
    def is_bottleneck_signal(self) -> bool:
        return (
            self.is_kneck
            or self.bottleneck_order_signal == "strong"
            or self.bottleneck_capacity_signal in ("expansion", "utilization_high")
            or self.bottleneck_margin_signal == "rising"
        )

    @property
    def emoji(self) -> str:
        return {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(self.direction, "⚪")

    @property
    def direction_label(self) -> str:
        return {"bullish": "↗ 利好", "bearish": "↘ 利空", "neutral": "→ 中性"}.get(
            self.direction, "→ 中性"
        )

    @property
    def category_emoji(self) -> str:
        return {
            "policy": "📜",
            "order": "📦",
            "capacity": "🏭",
            "financial": "💰",
            "patent": "🔬",
            "supply_disruption": "⚠️",
            "general": "📰",
        }.get(self.news_category, "📰")

    @property
    def badge(self) -> str:
        parts: List[str] = []
        if self.is_kneck:
            parts.append("🔧卡脖子")
        if self.bottleneck_order_signal == "strong":
            parts.append("📈订单爆发")
        if self.bottleneck_margin_signal == "rising":
            parts.append("💹毛利率↑")
        if self.bottleneck_capacity_signal == "utilization_high":
            parts.append("⚡满产")
        return " ".join(parts) if parts else ""

    @property
    def kness_pillars_label(self) -> str:
        labels = {
            "tech_moat": "技术代差",
            "single_point": "单点刚需",
            "certification": "3-5年认证",
            "long_cycle": "长周期",
        }
        return " / ".join(labels.get(p, p) for p in self.scarcity_pillars)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))