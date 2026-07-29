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


NEWS_ANALYSIS_PROMPT = """\u4f60\u662f A \u80a1\u5e02\u573a\u5206\u6790\u5e08\u3002\u8bf7\u9605\u8bfb\u4ee5\u4e0b\u65b0\u95fb\uff0c\u7ed9\u51fa\u4e25\u683c JSON \u5206\u6790\uff1a

\u3010\u65b0\u95fb\u6807\u9898\u3011 {title}
\u3010\u65b0\u95fb\u5185\u5bb9\u3011 {content}

\u8f93\u51fa\uff08\u5fc5\u987b\u662f\u5408\u6cd5 JSON\uff0c\u4e0d\u8981 markdown \u5305\u88c5\uff09:
{{
  "summary": "<\u4e00\u53e5\u8bdd\u603b\u7ed3\uff0c\u226430\u5b57>",
  "sectors": ["<\u884c\u4e1a\u677f\u5757\u5927\u7c7b1>", "<\u884c\u4e1a\u677f\u5757\u5927\u7c7b\u00d72>"],
  "stocks": ["<\u80a1\u7968\u540d\u6216\u4ee3\u7801>"],
  "direction": "<bullish | bearish | neutral>",
  "confidence": <0.0-1.0>,
  "time_horizon": "<intraday | next_day | weekly>",
  "rationale": "<\u226480\u5b57\u63a8\u7406>"
}}

\u89c4\u5219\uff1a
- \u53ea\u5728\u660e\u786e\u653f\u7b56/\u6570\u636e/\u4e8b\u4ef6\u65f6\u7ed9 bullish/bearish\uff0c\u6a21\u7cca\u65f6\u7ed9 neutral + confidence < 0.5
- \u4e0d\u8981\u7ed9\u6295\u8d44\u5efa\u8bae\uff0c\u53ea\u505a\u4e8b\u5b9e\u5206\u6790
- sectors \u5fc5\u987b\u662f\u884c\u4e1a\u5927\u7c7b\uff08\u534a\u5bfc\u4f53/\u767d\u9152/\u94f6\u884c\u7b49\uff09\uff0c\u4e0d\u8981\u7ed9\u4e2a\u80a1
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