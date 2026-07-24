import datetime
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.config import HoldingConfig
from app.fetcher import StockQuote
from app.storage import PriceDB

logger = logging.getLogger(__name__)


def is_market_open() -> bool:
    """Check if A-share market is currently trading (UTC+8)."""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    # Weekends
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    # Trading hours
    morning_start = now.replace(hour=9, minute=30, second=0)
    morning_end = now.replace(hour=11, minute=30, second=0)
    afternoon_start = now.replace(hour=13, minute=0, second=0)
    afternoon_end = now.replace(hour=15, minute=0, second=0)

    if morning_start <= now <= morning_end:
        return True
    if afternoon_start <= now <= afternoon_end:
        return True
    return False


@dataclass
class Alert:
    stock_name: str
    code: str
    change_pct: float
    current_price: float
    direction: str


@dataclass
class StockHistory:
    quotes: deque = field(default_factory=lambda: deque(maxlen=10))


@dataclass
class HoldingPnl:
    code: str
    name: str
    current_price: float
    cost: float
    shares: int
    pnl_amount: float
    pnl_pct: float
    daily_pnl_amount: float = 0.0
    daily_pnl_pct: float = 0.0


@dataclass
class RiskAlert:
    stock_name: str
    code: str
    alert_type: str
    pnl_pct: float
    current_price: float
    message: str


class PriceMonitor:
    def __init__(
        self,
        alert_threshold_pct: float = 2.0,
        sudden_threshold_pct: float = 1.0,
        stop_loss_pct: float = -8.0,
        take_profit_pct: float = 15.0,
        consecutive_decline_threshold: int = 4,
        db_path: str = "price_history.db",
    ):
        self.alert_threshold_pct = alert_threshold_pct
        self.sudden_threshold_pct = sudden_threshold_pct
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self._consecutive_decline_threshold = consecutive_decline_threshold
        self._history: Dict[str, StockHistory] = {}
        self._alerts: deque = deque(maxlen=50)
        self._initialized: bool = False
        self._consecutive_counts: Dict[str, int] = {}
        self._prev_change_pct: Dict[str, float] = {}
        self._db = PriceDB(db_path)
        self._prev_prices: Dict[str, float] = {}
        self._prev_volumes: Dict[str, float] = {}
        self._prev_drop: Dict[str, float] = {}
        self._acceleration_threshold: float = 1.5
        self._volume_spike_threshold: float = 2.0
        self._last_alert_time: Dict[str, float] = {}
        self._alert_cooldown: int = 1800  # 30 minutes in seconds

    def _should_alert(self, alert_type: str, code: str) -> bool:
        """Check if enough time has passed since last similar alert."""
        key = f"{code}:{alert_type}"
        now = time.time()
        last = self._last_alert_time.get(key, 0.0)
        if now - last < self._alert_cooldown:
            return False
        self._last_alert_time[key] = now
        return True

    def update(self, quotes: List[StockQuote]) -> List[Alert]:
        alerts: List[Alert] = []
        threshold_alert_codes: set = set()

        for quote in quotes:
            if quote.code not in self._history:
                self._history[quote.code] = StockHistory()

            history = self._history[quote.code]
            history.quotes.append(quote)

            if not self._initialized:
                continue

            if abs(quote.change_pct) >= self.alert_threshold_pct:
                if not self._should_alert("阈值", quote.code):
                    continue
                direction = "涨" if quote.change_pct > 0 else "跌"
                alert = Alert(
                    stock_name=quote.name,
                    code=quote.code,
                    change_pct=quote.change_pct,
                    current_price=quote.current_price,
                    direction=direction,
                )
                alerts.append(alert)
                threshold_alert_codes.add(quote.code)
                self._alerts.append(alert)

            if len(history.quotes) >= 4:
                prev_quotes = list(history.quotes)[-4:-1]
                avg_change_pct = sum(q.change_pct for q in prev_quotes) / len(prev_quotes)
                deviation = abs(quote.change_pct - avg_change_pct)
                if deviation >= self.sudden_threshold_pct:
                    if quote.code in threshold_alert_codes:
                        continue
                    if not self._should_alert("异动", quote.code):
                        continue
                    direction = "异动涨" if quote.change_pct > avg_change_pct else "异动跌"
                    alert = Alert(
                        stock_name=quote.name,
                        code=quote.code,
                        change_pct=quote.change_pct,
                        current_price=quote.current_price,
                        direction=direction,
                    )
                    alerts.append(alert)
                    self._alerts.append(alert)

        self._initialized = True
        return alerts

    def update_holdings(
        self,
        quotes: List[StockQuote],
        holdings: List[HoldingConfig],
    ) -> Tuple[List[HoldingPnl], List[RiskAlert]]:
        pnl_list: List[HoldingPnl] = []
        risk_alerts: List[RiskAlert] = []
        quote_map = {q.code: q for q in quotes}

        for holding in holdings:
            quote = quote_map.get(holding.code)
            if quote is None:
                continue
            if holding.cost is None or holding.shares is None:
                continue

            pnl_amount = (quote.current_price - holding.cost) * holding.shares
            pnl_pct = (quote.current_price - holding.cost) / holding.cost * 100

            daily_pnl_amount = (quote.current_price - quote.yesterday_close) * holding.shares
            daily_pnl_pct = (quote.current_price - quote.yesterday_close) / quote.yesterday_close * 100 if quote.yesterday_close > 0 else 0.0

            pnl = HoldingPnl(
                code=holding.code,
                name=holding.name,
                current_price=quote.current_price,
                cost=holding.cost,
                shares=holding.shares,
                pnl_amount=round(pnl_amount, 2),
                pnl_pct=round(pnl_pct, 2),
                daily_pnl_amount=round(daily_pnl_amount, 2),
                daily_pnl_pct=round(daily_pnl_pct, 2),
            )
            pnl_list.append(pnl)

            sl = holding.stop_loss if holding.stop_loss is not None else self.stop_loss_pct
            tp = holding.take_profit if holding.take_profit is not None else self.take_profit_pct

            if pnl_pct <= sl and self._should_alert("止损", holding.code):
                risk_alerts.append(RiskAlert(
                    stock_name=holding.name,
                    code=holding.code,
                    alert_type="止损",
                    pnl_pct=pnl_pct,
                    current_price=quote.current_price,
                    message=f"{holding.name} 亏损 {abs(pnl_pct):.1f}%，触发止损线",
                ))

            if pnl_pct >= tp and self._should_alert("止盈", holding.code):
                risk_alerts.append(RiskAlert(
                    stock_name=holding.name,
                    code=holding.code,
                    alert_type="止盈",
                    pnl_pct=pnl_pct,
                    current_price=quote.current_price,
                    message=f"{holding.name} 盈利 {pnl_pct:.1f}%，触发止盈线",
                ))

        return pnl_list, risk_alerts

    def check_consecutive(self, quotes: List[StockQuote]) -> List[RiskAlert]:
        risk_alerts: List[RiskAlert] = []
        for quote in quotes:
            prev = self._prev_change_pct.get(quote.code, 0.0)
            current = quote.change_pct
            prev_sign = 1 if prev > 0 else (-1 if prev < 0 else 0)
            current_sign = 1 if current > 0 else (-1 if current < 0 else 0)

            if current_sign == 0:
                self._consecutive_counts[quote.code] = 0
            elif current_sign == prev_sign:
                self._consecutive_counts[quote.code] = self._consecutive_counts.get(quote.code, 0) + 1
            else:
                self._consecutive_counts[quote.code] = 1

            self._prev_change_pct[quote.code] = current
            count = self._consecutive_counts.get(quote.code, 0)
            if count >= self._consecutive_decline_threshold:
                alert_type = "连续上涨" if current > 0 else "连续下跌"
                if self._should_alert(alert_type, quote.code):
                    risk_alerts.append(RiskAlert(
                        stock_name=quote.name,
                        code=quote.code,
                        alert_type=alert_type,
                        pnl_pct=current,
                        current_price=quote.current_price,
                        message=f"{quote.name} {alert_type} {count} 轮",
                    ))
        return risk_alerts

    def save_history(self, quotes: List[StockQuote]) -> None:
        self._db.save_quotes(quotes)

    def check_trend(self, quotes: List[StockQuote]) -> List[RiskAlert]:
        alerts: List[RiskAlert] = []
        for q in quotes:
            short_ma = self._db.calc_sma(q.code, 5)
            long_ma = self._db.calc_sma(q.code, 20)
            if short_ma is None or long_ma is None:
                continue
            prev_prices = self._db.get_recent(q.code, 2)
            if len(prev_prices) >= 2:
                prev_price = prev_prices[-1][0]
                if prev_price <= short_ma and q.current_price > short_ma and self._should_alert("趋势转多", q.code):
                    alerts.append(RiskAlert(
                        stock_name=q.name, code=q.code, alert_type="趋势转多",
                        pnl_pct=q.change_pct, current_price=q.current_price,
                        message=f"{q.name} 上穿均线，趋势转多",
                    ))
                elif prev_price >= short_ma and q.current_price < short_ma and self._should_alert("趋势转空", q.code):
                    alerts.append(RiskAlert(
                        stock_name=q.name, code=q.code, alert_type="趋势转空",
                        pnl_pct=q.change_pct, current_price=q.current_price,
                        message=f"{q.name} 下穿均线，趋势转空",
                    ))
        return alerts

    def get_trend(self, code: str) -> str:
        prices = self._db.get_recent(code, 3)
        if len(prices) < 3:
            return "--"
        if prices[0][0] > prices[1][0] > prices[2][0]:
            return "↗︎ 上涨"
        elif prices[0][0] < prices[1][0] < prices[2][0]:
            return "↘︎ 下跌"
        else:
            return "→ 震荡"

    def check_acceleration(self, quotes: List[StockQuote]) -> List[RiskAlert]:
        alerts: List[RiskAlert] = []
        for q in quotes:
            prev_price = self._prev_prices.get(q.code)
            if prev_price is not None:
                price_change_pct = (q.current_price - prev_price) / prev_price * 100
                if price_change_pct < 0:
                    prev_drop = self._prev_drop.get(q.code, 0.0)
                    if prev_drop < 0 and price_change_pct < prev_drop:
                        acceleration = abs(price_change_pct) - abs(prev_drop)
                        if acceleration >= self._acceleration_threshold and self._should_alert("加速下跌", q.code):
                            alerts.append(RiskAlert(
                                stock_name=q.name,
                                code=q.code,
                                alert_type="加速下跌",
                                pnl_pct=price_change_pct,
                                current_price=q.current_price,
                                message=f"{q.name} 加速下跌 {abs(price_change_pct):.1f}%",
                            ))
                    self._prev_drop[q.code] = price_change_pct
                else:
                    self._prev_drop[q.code] = 0.0

                prev_vol = self._prev_volumes.get(q.code, 0.0)
                if prev_vol > 0 and q.volume > 0:
                    vol_ratio = q.volume / prev_vol
                    if vol_ratio >= self._volume_spike_threshold and price_change_pct < 0 and self._should_alert("放量下跌", q.code):
                        alerts.append(RiskAlert(
                            stock_name=q.name,
                            code=q.code,
                            alert_type="放量下跌",
                            pnl_pct=price_change_pct,
                            current_price=q.current_price,
                            message=f"{q.name} 放量 {vol_ratio:.1f}倍 下跌 {abs(price_change_pct):.1f}%",
                        ))
                self._prev_volumes[q.code] = q.volume
            self._prev_prices[q.code] = q.current_price
        return alerts

    @property
    def total_pnl(self) -> float:
        return 0.0

    @property
    def total_pnl_pct(self) -> float:
        return 0.0

    def get_history(self, code: str) -> List[StockQuote]:
        if code not in self._history:
            return []
        return list(self._history[code].quotes)

    def get_recent_alerts(self, n: int = 10) -> List[Alert]:
        return list(self._alerts)[-n:]
