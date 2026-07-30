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
    # ===== A. 政策与监管（高确信度） =====
    "央行": 1.0, "证监会": 1.0, "国务院": 1.0, "政治局": 1.0, "统计局": 0.9,
    "降准": 1.0, "降息": 1.0, "加息": 1.0, "下调存款准备金率": 1.0,
    "GDP": 0.9, "CPI": 0.9, "PPI": 0.9, "PMI": 0.9, "M2": 0.8, "社融": 0.8,
    "补贴": 0.8, "退税": 0.7, "减税": 0.7, "国务院": 1.0, "工信部": 0.9,
    "发改委": 0.9, "商务部": 0.8,

    # ===== B. 卡脖子/稀缺性四柱（核心哲学，最高权重） =====
    "卡脖子": 1.0, "国产替代": 0.9, "紧缺": 0.9, "缺货": 0.9, "断供": 0.9,
    "瓶颈": 0.8, "技术壁垒": 0.8, "稀缺": 0.7, "市占率": 0.7, "垄断": 0.7,
    "供应链": 0.6, "产业链": 0.6, "上游": 0.5, "下游": 0.5,

    # ===== C. 三硬指标：订单/产能/价格 =====
    "订单": 0.7, "大单": 0.8, "排产": 0.8, "排产期": 0.9, "排满": 0.8,
    "扩产": 0.7, "投产": 0.7, "下线": 0.6, "量产": 0.7,
    "产能": 0.7, "满产": 0.9, "产能利用率": 0.9, "库存": 0.5,
    "涨价": 0.6, "提价": 0.6, "溢价": 0.7, "毛利率": 0.7,
    "中标": 0.7, "签约": 0.6, "采购": 0.5,
    "回购": 0.6, "增持": 0.6, "分红": 0.5,

    # ===== D. 财报与业绩指引 =====
    "业绩": 0.7, "财报": 0.7, "盈利": 0.6, "营收": 0.6, "净利润": 0.7,
    "指引": 0.7, "预期": 0.5, "超预期": 0.7, "指引上调": 0.8, "指引下调": -0.5,

    # ===== E. 行业龙头/主题词（卡脖子投资哲学的核心标的类型） =====
    "龙头": 0.5, "核心标的": 0.6, "独角兽": 0.6, "全球第一": 0.7, "全球领先": 0.7,
    "首发": 0.6, "突破": 0.6, "壁垒": 0.6, "护城河": 0.6,

    # ===== F. 高景气赛道主题（AI/光通信/机器人/半导体/新能源） =====
    "AI算力": 0.8, "算力": 0.7, "大模型": 0.7, "GPT": 0.6, "AI芯片": 0.8,
    "GPU": 0.6, "HBM": 0.8, "高带宽存储": 0.8,
    "CPO": 0.8, "光模块": 0.7, "800G": 0.8, "1.6T": 0.9, "硅光子": 0.8, "SiPh": 0.7,
    "磷化铟": 0.9, "InP": 0.8, "砷化镓": 0.8, "GaAs": 0.7, "化合物半导体": 0.8,
    "半导体设备": 0.7, "光刻机": 0.9, "EUV": 0.8, "刻蚀机": 0.7, "薄膜沉积": 0.7,
    "人形机器人": 0.8, "具身智能": 0.8, "减速器": 0.6, "丝杠": 0.7,
    "脑机接口": 0.8, "BCI": 0.7,
    "固态电池": 0.7, "钠离子电池": 0.7,
    "核电": 0.7, "可控核聚变": 0.8,
    "卫星互联网": 0.7, "6G": 0.7, "星链": 0.5,
    "量子计算": 0.8, "量子通信": 0.8,
    "低空经济": 0.7, "eVTOL": 0.7, "飞行汽车": 0.7,
    "生物制造": 0.7, "合成生物": 0.7,
    "数据要素": 0.7, "数据资产": 0.7,

    # ===== G. 一般重大事件 =====
    "突发": 1.0, "重大": 0.9, "确认": 0.8, "正式": 0.5, "落地": 0.6,
    "重组": 0.7, "收购": 0.7, "并购": 0.7, "上市": 0.6, "备案": 0.6, "发行": 0.5,
    "授权": 0.6, "认证": 0.6, "通过验证": 0.7, "进入供应链": 0.7,

    # ===== H. 海外/噪声黑名单（强烈负权重） =====
    "美股": -0.5, "港股": -0.5, "欧股": -0.5, "日股": -0.5, "韩股": -0.4,
    "美国": -0.3, "纳斯达克": -0.5, "道琼斯": -0.5, "标普": -0.4,
    "美光": -0.5, "英伟达": -0.4, "AMD": -0.3, "特斯拉": -0.3, "苹果": -0.3,
    "美玉米": -0.7, "芝加哥": -0.5, "高盛": -0.3, "摩根": -0.3,
    "邀请": -0.5, "福利": -0.5, "抽奖": -0.8, "直播": -0.5,
    "广告": -0.7, "活动": -0.3, "促销": -0.5,
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
  "stocks": [
    {{"code": "sh601398", "name": "工商银行"}},
    {{"code": "sh600519", "name": "贵州茅台"}}
  ],
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
- 不要给投资建议，只做事实分析
- sectors 必须是行业大类（半导体/白酒/银行等），不要给个股
- news_category 必填，决定下游通知权重
- narrative_themes 用简短标签（如 "AI算力"、"CPO"、"国产替代"），≤5 个
- is_kneck 仅在新闻明显涉及卡脖子/单点关键环节时设为 true

### 置信度校准（必须严格遵守，超严格）

按以下分档给 direction 和 confidence：

| 档位 | confidence | direction 要求 |
|------|-----------|---------------|
| **噪声**（信息不足、纯观点、传闻） | 0.0 - 0.4 | **必须** neutral；不要写具体板块 |
| **模糊**（侧面对接、未明确表态） | 0.45 - 0.65 | **应该** neutral；若要 bullish/bearish 必须新闻极清晰 |
| **明确**（具体数字、明确政策、落地事件） | 0.70 - 0.85 | 可 bullish/bearish |
| **高确信**（央行/部委级数据、明确订单金额） | 0.90 - 1.00 | bullish/bearish 均可 |

**硬性规则**：
- confidence < 0.5 → direction 必须是 neutral（下游会过滤掉低置信度信号）
- confidence ≥ 0.7 才能给 bullish/bearish，否则一律 neutral
- 模糊信号宁可给 neutral + 0.3，也不要硬给 0.65（用户会被误导）
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
        raw = self._call_raw_with_payload(prompt, payload)
        if raw is None:
            return None
        try:
            analysis = NewsAnalysis.from_json(news.hash, raw)
        except (ValueError, KeyError) as e:
            logger.warning(f"[OllamaAnalyzer] parse failed: {e}")
            return None
        logger.debug(f"[OllamaAnalyzer] {self._model} for {news.hash}")
        return analysis

    def _call_raw(self, prompt: str) -> Optional[str]:
        """Lower-level: send prompt, return raw response string."""
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }
        return self._call_raw_with_payload(prompt, payload)

    def _call_raw_with_payload(self, prompt: str, payload: dict) -> Optional[str]:
        if self._auth_failed:
            return None
        try:
            with self._lock:
                resp = requests.post(
                    f"{self._host}/api/generate",
                    headers=self._headers(),
                    json=payload,
                    timeout=self._timeout,
                )
            if resp.status_code in (401, 403):
                self._auth_failed = True
                logger.error(
                    f"[OllamaAnalyzer] auth failed ({resp.status_code}); disabling"
                )
                return None
            resp.raise_for_status()
            return resp.json().get("response", "")
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