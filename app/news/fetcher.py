"""CLS (财联社) news fetcher via official JSON API.

实测: telegraphList endpoint 不强制校验 sign，可省略。
参考 https://www.cls.cn/telegraph 浏览器 Network 抓包。
"""
import hashlib
import logging
from typing import List, Optional

import requests

from app.news.models import RawNews

logger = logging.getLogger(__name__)

CLS_URL = "https://www.cls.cn/api/cache"

CLS_HEADERS = {
    "referer": "https://www.cls.cn/telegraph",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
}

NO_PROXY = {"http": "", "https": ""}


class ClsFetcher:
    def __init__(
        self,
        sign: Optional[str] = None,
        cookie: Optional[str] = None,
        timeout: int = 10,
    ):
        self._sign = sign
        self._cookie = cookie
        self._timeout = timeout
        self._last_time: Optional[int] = None

    def fetch(self, max_items: int = 50) -> List[RawNews]:
        params: dict = {
            "app": "CailianpressWeb",
            "name": "telegraphList",
            "os": "web",
            "sv": "8.7.9",
        }
        if self._sign:
            params["sign"] = self._sign
        if self._last_time:
            params["lastTime"] = str(self._last_time)

        headers = dict(CLS_HEADERS)
        if self._cookie:
            headers["cookie"] = self._cookie

        try:
            resp = requests.get(
                CLS_URL,
                params=params,
                headers=headers,
                timeout=self._timeout,
                proxies=NO_PROXY,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as e:
            logger.warning(f"[ClsFetcher] network error: {e}")
            return []
        except ValueError as e:
            logger.warning(f"[ClsFetcher] JSON parse error: {e}")
            return []

        if payload.get("errno") != 0:
            logger.warning(
                f"[ClsFetcher] errno={payload.get('errno')} msg={payload.get('msg')}"
            )
            return []

        data = payload.get("data") or {}
        items = (data.get("roll_data") or []) + (data.get("vip") or [])
        if not items:
            return []

        # 增量：推进 last_time 到最新条目 ctime
        newest = max(i.get("ctime", 0) for i in items)
        if self._last_time is None or newest > self._last_time:
            self._last_time = newest

        return [self._to_raw_news(i) for i in items[:max_items]]

    @staticmethod
    def _to_raw_news(item: dict) -> RawNews:
        title = item.get("title", "")
        content = item.get("content") or item.get("brief") or ""
        digest = hashlib.sha256(f"{title}|{content}".encode("utf-8")).hexdigest()[:16]
        return RawNews(
            id=int(item.get("id", 0)),
            title=title,
            content=content,
            ctime=int(item.get("ctime", 0)),
            type=item.get("type_name", ""),
            url=f"https://www.cls.cn/detail/{item.get('id')}" if item.get("id") else "",
            hash=digest,
        )