"""CLS digest fetcher + LLM impact analyzer.

Pulls /api/csw 'news digest' items (morning/noon/evening summaries),
runs specialized LLM analysis to assess impact on:
  - overall market sentiment
  - specific sectors
  - the user's holdings

Runs less frequently than the main news pipeline (every few hours)
because digests are curated summaries, not raw firehose.
"""
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from app.news.models import Stock

logger = logging.getLogger(__name__)


CLS_DIGEST_URL = "https://www.cls.cn/api/csw"

CLS_DIGEST_HEADERS = {
    "referer": "https://www.cls.cn/telegraph",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://www.cls.cn",
}

NO_PROXY = {"http": "", "https": ""}

DIGEST_PROMPT = """你是 A 股市场资深分析师。以下是最近 1 天财联社的早间/午间/晚间新闻精选汇总。
请分析这些新闻对【当前 A 股市场、特定行业板块、我的持仓股】的影响。

【我的持仓股】
{holdings}

【新闻精选汇总】
{digests}

请按"卡脖子供应链瓶颈理论"分析。给出严格 JSON:

{{
  "summary": "<3 句话以内的整体研判>",
  "market_sentiment": "<bullish | bearish | neutral | volatile>",
  "market_confidence": <0.0-1.0>,
  "sector_impacts": [
    {{"sector": "<行业大类>", "direction": "<bullish|bearish|neutral>",
     "magnitude": "<high|medium|low>", "reason": "<一句话事实>"}}
  ],
  "holdings_impacts": [
    {{"code": "sh601398", "name": "工商银行",
     "impact": "<positive|negative|neutral>", "confidence": <0.0-1.0>,
     "reason": "<一句话事实>"}}
  ],
  "key_events": ["<2-5 条最重要事件>"],
  "narrative_themes": ["<AI算力|CPO|国产替代|...>"],
  "rationale": "<≤150字综合推理>"
}}

### 置信度校准（严格）

| 档位 | confidence | 含义 |
|------|-----------|------|
| 噪声 | 0.0-0.4 | 必须 market_sentiment=neutral |
| 模糊 | 0.45-0.65 | 应该 neutral |
| 明确 | 0.70-0.85 | 可给出 directional |
| 高确信 | 0.90-1.00 | 央行/部委级数据 / 明确订单 |

### 规则
- 只对持仓股和明确新闻提及的板块给 impact，其他不写
- holdings_impacts 按 impact 强度排序，最显著在前
- sector_impacts 只列 bullish/bearish 的，不要 noise
- 不要给投资建议，只做事实分析
- 不要 markdown 包装
"""


@dataclass
class Digest:
    """A single CLS news digest item (早间/午间/晚间)."""

    id: int
    title: str
    digest_type: str            # "morning" / "noon" / "evening"
    digest_date: str            # "2026-07-29"
    ctime: int
    content: str
    hash: str = ""

    def __post_init__(self):
        if not self.hash:
            self.hash = hashlib.sha256(
                f"{self.title}|{self.content}".encode("utf-8")
            ).hexdigest()[:16]


@dataclass
class DigestAnalysis:
    """LLM output for a digest batch impact assessment."""

    digest_hashes: List[str]               # all digests covered
    summary: str
    market_sentiment: str = "neutral"
    market_confidence: float = 0.0
    sector_impacts: List[dict] = field(default_factory=list)
    holdings_impacts: List[dict] = field(default_factory=list)
    key_events: List[str] = field(default_factory=list)
    narrative_themes: List[str] = field(default_factory=list)
    rationale: str = ""
    analyzed_at: float = field(default_factory=time.time)

    @classmethod
    def from_json(cls, digest_hashes: List[str], raw: "str | dict") -> "DigestAnalysis":
        import re
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    data = json.loads(m.group(0))
                else:
                    raise
        return cls(
            digest_hashes=digest_hashes,
            summary=str(data.get("summary", ""))[:300],
            market_sentiment=str(data.get("market_sentiment", "neutral")).lower(),
            market_confidence=_clip(float(data.get("market_confidence", 0.0))),
            sector_impacts=data.get("sector_impacts", [])[:10],
            holdings_impacts=data.get("holdings_impacts", [])[:20],
            key_events=[str(e)[:100] for e in data.get("key_events", [])][:5],
            narrative_themes=[str(t)[:20] for t in data.get("narrative_themes", [])][:5],
            rationale=str(data.get("rationale", ""))[:300],
        )

    @property
    def has_holdings_impact(self) -> bool:
        return any(
            isinstance(i, dict) and i.get("impact") in ("positive", "negative")
            for i in self.holdings_impacts
        )

    @property
    def strongest_holdings_impact(self) -> Optional[dict]:
        for i in self.holdings_impacts:
            if isinstance(i, dict) and i.get("impact") in ("positive", "negative"):
                return i
        return None


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


_DIGEST_TYPE_PATTERNS = [
    (re.compile(r"早间"), "morning"),
    (re.compile(r"午间"), "noon"),
    (re.compile(r"晚间"), "evening"),
]


def _parse_digest_type(title: str) -> str:
    for pattern, dtype in _DIGEST_TYPE_PATTERNS:
        if pattern.search(title):
            return dtype
    return "unknown"


def _parse_digest_date(ctime: int) -> str:
    """Extract YYYY-MM-DD in UTC+8 from unix timestamp."""
    dt = datetime.fromtimestamp(ctime, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d")


class DigestFetcher:
    """Fetches CLS news digests via /api/csw."""

    def __init__(self, sign: Optional[str] = None, cookie: Optional[str] = None,
                 timeout: int = 10):
        self._sign = sign
        self._cookie = cookie
        self._timeout = timeout
        self._last_time: Optional[int] = None

    def fetch(self, max_pages: int = 3, page_size: int = 30) -> List[Digest]:
        """Fetch digest items via lastTime pagination (max 3 pages = ~90 items)."""
        all_digests: List[Digest] = []
        cursor = self._last_time
        for page in range(max_pages):
            params = {
                "app": "CailianpressWeb",
                "os": "web",
                "sv": "8.7.9",
            }
            if self._sign:
                params["sign"] = self._sign
            payload = {
                "keyword": "新闻精选",
                "category": "red",
            }
            if cursor:
                payload["lastTime"] = int(cursor)

            headers = dict(CLS_DIGEST_HEADERS)
            if self._cookie:
                headers["cookie"] = self._cookie
            try:
                resp = requests.post(
                    CLS_DIGEST_URL,
                    params=params,
                    json=payload,
                    headers=headers,
                    timeout=self._timeout,
                    proxies=NO_PROXY,
                )
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                logger.warning(f"[DigestFetcher] page {page} failed: {e}")
                break

            if data.get("error"):
                logger.warning(f"[DigestFetcher] API error: {data['error']}")
                break

            items = (data.get("list") or [])
            if not items:
                break

            for item in items:
                ctime = int(item.get("ctime", 0))
                if not ctime:
                    continue
                all_digests.append(Digest(
                    id=int(item.get("id", 0)),
                    title=item.get("title", ""),
                    digest_type=_parse_digest_type(item.get("title", "")),
                    digest_date=_parse_digest_date(ctime),
                    ctime=ctime,
                    content=item.get("content", ""),
                ))

            cursor = min(i.get("ctime", 0) for i in items)
            if self._last_time is None or cursor < self._last_time:
                self._last_time = cursor

        return all_digests


class DigestAnalyzer:
    """LLM impact analysis for a batch of digests."""

    def __init__(self, llm: "OllamaAnalyzer"):
        self._llm = llm

    def analyze(
        self,
        digests: List[Digest],
        holdings: List[Stock],
    ) -> Optional["DigestAnalysis"]:
        if not digests:
            return None
        holdings_text = "\n".join(
            f"- {s.code} {s.name}" for s in holdings if s.code
        ) or "（无持仓）"
        digests_text = "\n\n".join(
            f"【{d.digest_type}】{d.title}\n{d.content[:800]}"
            for d in digests
        )
        prompt = DIGEST_PROMPT.format(
            holdings=holdings_text,
            digests=digests_text,
        )
        raw = self._llm._call_raw(prompt)
        if raw is None:
            return None
        try:
            hashes = [d.hash for d in digests]
            return DigestAnalysis.from_json(hashes, raw)
        except (ValueError, KeyError) as e:
            logger.warning(f"[DigestAnalyzer] parse failed: {e}")
            return None