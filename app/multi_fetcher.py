"""
多数据源轮询 + 故障切换模块

支持腾讯 / 新浪 / 通达信 三路并行故障切换，
任意一路失败自动切换下一路，轮询顺序定期轮换防止单路触发风控。
"""

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol

import requests

logger = logging.getLogger(__name__)


@dataclass
class StockQuote:
    """行情数据快照。"""

    code: str
    name: str
    current_price: float
    yesterday_close: float
    change_pct: float
    change_amount: float
    volume: float = 0.0

# 动态检测 mootdx 是否可用（mootdx 0.11+ 改用 Quotes().factory()，但模块本身存在即视为可用）
HAS_MOOTDX = False
try:
    import mootdx
    HAS_MOOTDX = True
except ImportError:
    logger.info("mootdx not installed, TDXSource disabled")

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class StockSource(Protocol):
    """数据源协议，所有 Source 必须实现 fetch() 和 health_check()。"""

    name: str

    def fetch(self, codes: List[str]) -> List[StockQuote]:
        """抓取行情；失败必须抛出异常，不返回空列表。"""
        ...

    def health_check(self) -> bool:
        """快速探测连通性。"""
        ...


# ---------------------------------------------------------------------------
# TencentSource（封装现有 fetcher.py 逻辑）
# ---------------------------------------------------------------------------


class TencentSource:
    """腾讯行情 API，数据格式：v_sh600519="1~名称~代码~现价~昨收~...""" ""

    name = "Tencent"

    def __init__(self, api_template: str = "http://qt.gtimg.cn/q={codes}"):
        self._api_template = api_template

    def fetch(self, codes: List[str]) -> List[StockQuote]:
        if not codes:
            return []

        codes_str = ",".join(codes)
        url = self._api_template.format(codes=codes_str)
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return self._parse(response.text)

    def health_check(self) -> bool:
        try:
            return len(self.fetch(["sh000001"])) > 0
        except Exception:
            return False

    @staticmethod
    def _parse(text: str) -> List[StockQuote]:
        quotes: List[StockQuote] = []
        if not text:
            return quotes

        for entry in text.strip().split(";"):
            entry = entry.strip()
            if not entry:
                continue

            match = re.match(r'v_(sh\d+|sz\d+)="(.*)"', entry)
            if not match:
                continue

            code = match.group(1)
            fields = match.group(2).split("~")

            if len(fields) < 5:
                continue

            try:
                name = fields[1] if fields[1] else code
                current_price = float(fields[3]) if fields[3] else 0.0
                yesterday_close = float(fields[4]) if fields[4] else 0.0

                change_pct = 0.0
                if len(fields) > 32 and fields[32]:
                    try:
                        change_pct = float(fields[32])
                    except ValueError:
                        pass
                if change_pct == 0.0 and yesterday_close > 0:
                    change_pct = (current_price - yesterday_close) / yesterday_close * 100

                change_amount = current_price - yesterday_close

                volume = 0.0
                if len(fields) > 6 and fields[6]:
                    try:
                        volume = float(fields[6])
                    except ValueError:
                        pass

                quotes.append(StockQuote(
                    code=code,
                    name=name,
                    current_price=current_price,
                    yesterday_close=yesterday_close,
                    change_pct=round(change_pct, 2),
                    change_amount=round(change_amount, 2),
                    volume=volume,
                ))
            except (ValueError, IndexError) as e:
                logger.warning(f"[TencentSource] parse error for {code}: {e}")
                continue

        return quotes


# ---------------------------------------------------------------------------
# TDXSource（通达信 / mootdx 0.11+）
# ---------------------------------------------------------------------------


class TDXSource:
    """通达信行情 API，通过 mootdx 0.11+ 连接（TCP 7709）。
    mootdx 为可选依赖，未安装或初始化失败时 fetch 抛 ImportError 触发下一路切换。

    注：mootdx 0.11+ API 改用 `mootdx.quotes.Quotes().factory()`；返回 DataFrame
    字段 `last_close`（昨收，替代旧版的 `close`/`settlement`）；`name` 字段为 None，
    需要调用方通过 `name_map` 提供中文名。
    """

    name = "TDX"

    def __init__(self, name_map: Optional[Dict[str, str]] = None, timeout: int = 5):
        self._name_map = name_map or {}
        self._client: Optional["StdQuotes"] = None
        if not HAS_MOOTDX:
            return
        from mootdx.quotes import Quotes
        # mootdx 0.11+ 的默认 BESTIP.HQ 配置有 bug（空字符串而非空列表），
        # 会导致 `ip, port = self.server` 抛 ValueError。先尝试默认 discovery，
        # 失败则用下面的 fallback 服务器列表按顺序探测
        fallback_servers = [
            ("60.191.117.167", 7709),   # 上海电信
            ("180.153.18.170", 7709),   # 上海电信
            ("60.12.136.250", 7709),    # 杭州电信
        ]
        for server in [None] + fallback_servers:
            try:
                kwargs = {"market": "std", "timeout": timeout}
                if server is not None:
                    kwargs["server"] = server
                client = Quotes().factory(**kwargs)
                # 立即做一次轻量探活，避免 init 通过但实际连不通
                client.quotes(["600519"])
                self._client = client
                if server is not None:
                    logger.info(f"[TDXSource] connected via fallback server {server}")
                return
            except Exception as e:
                logger.debug(f"[TDXSource] server {server} failed: {e}")
                continue
        logger.warning("[TDXSource] all servers failed, TDX disabled")
        self._client = None

    def fetch(self, codes: List[str]) -> List[StockQuote]:
        if not HAS_MOOTDX or self._client is None:
            raise ImportError("mootdx not installed or TDX init failed")
        df = self._client.quotes(codes)
        return self._parse_df(df)

    def health_check(self) -> bool:
        if self._client is None:
            return False
        try:
            return len(self.fetch(["600519"])) > 0
        except Exception:
            return False

    def _parse_df(self, df) -> List[StockQuote]:
        quotes: List[StockQuote] = []
        try:
            import pandas as pd
            if not isinstance(df, pd.DataFrame) or df.empty:
                return quotes

            for _, row in df.iterrows():
                code = str(row.get("code", "")).strip()
                if not code:
                    continue

                # mootdx 0.11+ 返回纯数字代码 + market 列（1=SH, 0=SZ），
                # 直接用 market 加前缀；不靠首位数字猜（对指数会错）
                if not (code.startswith("sh") or code.startswith("sz")):
                    market = row.get("market", 1)
                    prefix = "sh" if int(market) == 1 else "sz"
                    code = f"{prefix}{code}"

                try:
                    current_price = float(row.get("price", 0) or 0)
                    yesterday_close = float(row.get("last_close", 0) or 0)

                    change_amount = current_price - yesterday_close
                    change_pct = 0.0
                    if yesterday_close > 0:
                        change_pct = (current_price - yesterday_close) / yesterday_close * 100

                    volume = float(row.get("vol", 0) or 0)
                    # mootdx 0.11+ 不返回 name，从调用方传入的 name_map 取（找不到则用 code 占位）
                    name = self._name_map.get(code, code)

                    quotes.append(StockQuote(
                        code=code,
                        name=name,
                        current_price=current_price,
                        yesterday_close=yesterday_close,
                        change_pct=round(change_pct, 2),
                        change_amount=round(change_amount, 2),
                        volume=volume,
                    ))
                except (ValueError, TypeError) as e:
                    logger.warning(f"[TDXSource] parse error for {code}: {e}")
                    continue
        except Exception as e:
            logger.warning(f"[TDXSource] DataFrame parse error: {e}")
        return quotes


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """熔断器：连续失败超过阈值后进入冷却期。

    - successive_fail_limit: 触发熔断的连续失败次数（默认 3 次）
    - cooldown_seconds:     冷却时长（默认 60 秒）
    """

    def __init__(
        self,
        successive_fail_limit: int = 3,
        cooldown_seconds: int = 60,
    ):
        self._fail_count: Dict[str, int] = defaultdict(int)
        self._last_fail_time: Dict[str, float] = {}
        self._successive_fail_limit = successive_fail_limit
        self._cooldown_seconds = cooldown_seconds

    def is_available(self, source_name: str) -> bool:
        """Source 是否可尝试（未触发熔断）。"""
        count = self._fail_count.get(source_name, 0)
        if count < self._successive_fail_limit:
            return True
        # 进入冷却期，检查是否已过冷却时间
        elapsed = time.time() - self._last_fail_time.get(source_name, 0)
        return elapsed >= self._cooldown_seconds

    def record_success(self, source_name: str) -> None:
        """成功时重置计数。"""
        self._fail_count[source_name] = 0

    def record_failure(self, source_name: str) -> None:
        """失败时增加计数。"""
        self._fail_count[source_name] += 1
        self._last_fail_time[source_name] = time.time()

    def status(self) -> Dict[str, bool]:
        """返回各 Source 的当前可用状态（供调试）。"""
        return {name: self.is_available(name) for name in self._fail_count}


# ---------------------------------------------------------------------------
# RotatingMultiFetcher
# ---------------------------------------------------------------------------


class RotatingMultiFetcher:
    """多数据源轮询 + 故障切换封装。

    行为：
    - 按轮换顺序尝试各路 Source（成功一次则该路排到队尾）
    - 单路失败 → CircuitBreaker 记录，连续 3 次失败后该路进入 60s 冷却期
    - 三路全失败 → 返回最近缓存数据 + 记录 ERROR

    使用示例：
        sources = [TencentSource(), TDXSource()]
        fetcher = RotatingMultiFetcher(sources)
        quotes = fetcher.fetch(["sh600519", "sz000001"])
    """

    def __init__(
        self,
        sources: List[StockSource],
        successive_fail_limit: int = 3,
        cooldown_seconds: int = 60,
    ):
        if not sources:
            raise ValueError("At least one source is required")
        self._sources = sources
        self._index = 0  # 当前优先 source 索引
        self._breaker = CircuitBreaker(
            successive_fail_limit=successive_fail_limit,
            cooldown_seconds=cooldown_seconds,
        )
        self._cache: Dict[str, StockQuote] = {}

    def fetch(self, codes: List[str]) -> List[StockQuote]:
        """按轮换顺序尝试各路 Source，成功则更新轮换索引。"""
        for offset in range(len(self._sources)):
            idx = (self._index + offset) % len(self._sources)
            source = self._sources[idx]

            if not self._breaker.is_available(source.name):
                logger.debug(f"[RotatingMultiFetcher] Circuit open for {source.name}, skipping")
                continue

            try:
                quotes = source.fetch(codes)
                if quotes:
                    # 成功：轮换到下一路，更新缓存
                    self._index = (idx + 1) % len(self._sources)
                    self._breaker.record_success(source.name)
                    self._update_cache(quotes)
                    logger.debug(f"[RotatingMultiFetcher] {source.name} succeeded, next index={self._index}")
                    return quotes
                else:
                    # 返回空列表也算失败
                    self._breaker.record_failure(source.name)
                    logger.warning(f"[RotatingMultiFetcher] {source.name} returned empty, trying next...")
            except ImportError as e:
                # TDXSource mootdx 未安装，直接跳过
                logger.warning(f"[RotatingMultiFetcher] {source.name} ImportError: {e}, skipping")
                self._breaker.record_failure(source.name)
            except Exception as e:
                logger.warning(f"[RotatingMultiFetcher] {source.name} failed: {e}, trying next...")
                self._breaker.record_failure(source.name)

        # 三路全失败，返回缓存
        logger.error("[RotatingMultiFetcher] All sources failed, returning cached data")
        return [self._cache[c] for c in codes if c in self._cache]

    def _update_cache(self, quotes: List[StockQuote]) -> None:
        for quote in quotes:
            self._cache[quote.code] = quote

    @property
    def circuit_status(self) -> Dict[str, bool]:
        """返回当前熔断器状态（用于调试）。"""
        return self._breaker.status()

    @property
    def current_source(self) -> str:
        """返回当前优先 Source 名称。"""
        return self._sources[self._index].name
