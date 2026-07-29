"""News analyzer: keyword pre-filter + Ollama LLM + JSON parsing + SQLite cache."""
import json
import logging
import os
import threading
import time
from typing import List, Optional

import requests

from app.news.models import NewsAnalysis, RawNews

logger = logging.getLogger(__name__)


HIGH_IMPACT_KEYWORDS = {
    "\u592e\u884c": 1.0, "\u8bc1\u76d1\u4f1a": 1.0, "\u56fd\u52a1\u9662": 1.0, "\u653f\u6cbb\u5c40": 1.0,
    "\u964d\u51c6": 1.0, "\u964d\u606f": 1.0, "\u52a0\u606f": 1.0, "\u4e0a\u8c03\u5b58\u6c3e\u51c6\u5907\u91d1\u7387": 1.0,
    "GDP": 0.9, "CPI": 0.9, "PPI": 0.9, "PMI": 0.9, "M2": 0.8,
    "\u7a81\u53d1": 1.0, "\u91cd\u5927": 0.9, "\u786e\u8ba4": 0.8, "\u6570\u636e": 0.7,
    "\u4e1a\u7ee9": 0.7, "\u8d22\u62a5": 0.7, "\u4e2d\u6807": 0.7, "\u6536\u8d2d": 0.7, "\u91cd\u7ec4": 0.7,
    "\u9881\u53d1": 0.7, "\u5907\u6848": 0.6, "\u4e0a\u5e02": 0.6, "\u53d1\u884c": 0.5,
    "\u9080\u8bf7": -0.5, "\u798f\u5229": -0.5, "\u62bd\u5956": -0.8, "\u76f4\u64ad": -0.3,
    "\u5e7f\u544a": -0.7, "\u6d3b\u52a8": -0.3,
}


def keyword_score(title: str, content: str) -> float:
    """Return the max keyword weight found in title+head(content)."""
    text = title + " " + content[:200]
    return max(
        (w for k, w in HIGH_IMPACT_KEYWORDS.items() if k in text),
        default=0.0,
    )


NEWS_ANALYSIS_PROMPT = """你是 A 股分析师 + 供应链瓶颈研究员。请按"卡脖子供应链瓶颈理论"分析以下新闻，给出严格 JSON。

【新闻标题】 {title}
【新闻内容】 {content}

输出（合法 JSON，不要 markdown 包装）:
{{
  "summary": "<一句话总结，≤30字>",
  "sectors": ["<行业板块大类>"],
  "stocks": ["<股票名或代码>"],
  "direction": "<bullish | bearish | neutral>",
  "confidence": <0.0-1.0>,
  "time_horizon": "<intraday | next_day | weekly>",
  "rationale": "<≤80字事实推理>",

  "news_category": "<policy|order|capacity|financial|patent|supply_disruption|general>",
  "bottleneck_order_signal": "<none|mentioned|strong>",
  "bottleneck_capacity_signal": "<none|expansion|utilization_high|inventory_warning>",
  "bottleneck_margin_signal": "<unknown|rising|stable|declining>",
  "is_kneck": <true|false>,
  "scarcity_pillars": ["<tech_moat|single_point|certification|long_cycle>"],
  "trend_horizon_years": <1-10>,
  "industry_certainty": "<speculative|emerging|established|dominant>",
  "narrative_themes": ["<AI算力|CPO|人形机器人|半导体设备|国产替代|光通信|特种气体|磷化铟|...>"]
}}

## 分析框架（卡脖子供应链瓶颈理论）

### 1. 三硬指标（订单/产能/毛利率）
- 订单爆发：龙头排产期是否排满？是否要溢价拿货？strong=订单合同明确+大单，mentioned=侧面提及
- 产能利用率：expansion=扩产在建/投产，utilization_high=满产/扩产周期，inventory_warning=扩产过快库存积压
- 毛利率：rising=稳中有升（卡脖子核心证据），stable=持平，declining=壁垒被破或价格战

### 2. 卡脖子四柱（is_kneck=true 时填 pillars）
- tech_moat：技术代差（需几十年工艺积累，非砸钱短期能追上）
- single_point：单点刚需（一旦断供下游整个行业停摆）
- certification：极高认证门槛（3-5 年验证周期，几乎永久的生意）
- long_cycle：长周期替代难度

### 3. 产业趋势判断
- 只看未来 5-10 年不可逆变化（地缘政治国产替代、AI 算力、人形机器人），不看当下热点
- certainty: speculative（投机）→ emerging（萌芽）→ established（确立）→ dominant（主导）

### 通用规则
- 仅在明确政策/数据/事件时给 bullish/bearish；模糊给 neutral + confidence < 0.5
- 不要给投资建议，只做事实分析
- sectors 必须是行业大类（半导体/白酒/银行等），不要给个股
- news_category 必填，决定下游通知权重
- narrative_themes 用简短标签（如 "AI算力"、"CPO"、"国产替代"），≤5 个
- is_kneck 仅在新闻明显涉及卡脖子/单点关键环节时设为 true
"""


class OllamaAnalyzer:
    """Ollama HTTP client for news analysis.

    Supports both local Ollama daemon and cloud models (need Authorization header).
    """

    def __init__(
        self,
        model: str = "minimax-m2.5:cloud",
        host: str = "http://localhost:11434",
        api_key: Optional[str] = None,
        timeout: int = 30,
    ):
        self._model = model
        self._host = host.rstrip("/")
        self._api_key = api_key or os.environ.get("OLLAMA_API_KEY") or os.environ.get("OLLAMA_KEY")
        self._timeout = timeout
        self._lock = threading.Lock()
        self._auth_failed = False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def health_check(self) -> bool:
        try:
            r = requests.get(
                f"{self._host}/api/tags",
                headers=self._headers(),
                timeout=3,
            )
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
            available = any(self._model in m for m in models)
            if not available:
                logger.warning(
                    f"[OllamaAnalyzer] model {self._model} not in: {models[:5]}"
                )
            return available
        except Exception as e:
            logger.debug(f"[OllamaAnalyzer] health check failed: {e}")
            return False

    def analyze(self, news: RawNews) -> Optional[NewsAnalysis]:
        """Return NewsAnalysis or None on failure."""
        if self._auth_failed:
            return None
        prompt = NEWS_ANALYSIS_PROMPT.format(
            title=news.title,
            content=(news.content or "")[:1000],
        )
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }
        try:
            with self._lock:
                t0 = time.time()
                resp = requests.post(
                    f"{self._host}/api/generate",
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout,
                )
            elapsed = time.time() - t0
            if resp.status_code in (401, 403):
                self._auth_failed = True
                logger.error(
                    f"[OllamaAnalyzer] auth failed ({resp.status_code}); disabling"
                )
                return None
            resp.raise_for_status()
            raw = resp.json().get("response", "")
            logger.debug(
                f"[OllamaAnalyzer] {self._model} took {elapsed:.1f}s for {news.hash}"
            )
            return NewsAnalysis.from_json(news.hash, raw)
        except requests.RequestException as e:
            logger.warning(f"[OllamaAnalyzer] request failed: {e}")
            return None
        except (ValueError, KeyError) as e:
            logger.warning(f"[OllamaAnalyzer] parse failed: {e}")
            return None


class TokenBucket:
    """Simple thread-safe token bucket for rate limiting."""

    def __init__(self, rate_per_minute: int):
        self._interval = 60.0 / rate_per_minute
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        """Block until a token is available."""
        while True:
            with self._lock:
                now = time.time()
                wait = self._last + self._interval - now
                if wait <= 0:
                    self._last = now
                    return
            time.sleep(min(wait, 1.0))