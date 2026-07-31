import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import AppKit
import objc
import rumps

from app.config import AppConfig, HoldingConfig, IndexConfig, save_config
from app.multi_fetcher import (
    RotatingMultiFetcher,
    TencentSource,
    TDXSource,
    HAS_MOOTDX,
    StockQuote,
)
from app.monitor import Alert, PriceMonitor, RiskAlert, is_market_open
from app.news.monitor import NewsMonitor
from app.storage import PriceDB

logger = logging.getLogger(__name__)

COLOR_UP = AppKit.NSColor.redColor()
COLOR_DOWN = AppKit.NSColor.colorWithRed_green_blue_alpha_(0.0, 0.8, 0.0, 1.0)
COLOR_FLAT = AppKit.NSColor.whiteColor()
FONT = AppKit.NSFont.menuFontOfSize_(13)


def _set_attributed_title(item: rumps.MenuItem, title: str, color) -> None:
    attr_str = AppKit.NSMutableAttributedString.alloc().initWithString_(title)
    length = len(title)
    attr_str.addAttributes_range_(
        {
            AppKit.NSForegroundColorAttributeName: color,
            AppKit.NSFontAttributeName: FONT,
        },
        (0, length),
    )
    item._menuitem.setAttributedTitle_(attr_str)


def _color_for_change_pct(change_pct: float):
    if change_pct > 0:
        return COLOR_UP
    elif change_pct < 0:
        return COLOR_DOWN
    else:
        return COLOR_FLAT


class _UIUpdater(AppKit.NSObject):
    def initWithApp_(self, app):
        self = objc.super(_UIUpdater, self).init()
        if self is not None:
            self._app = app
        return self

    def updateUI_(self, timer):
        self._app._update_ui()


class StockMenuBarApp(rumps.App):
    def __init__(self, config: AppConfig):
        super().__init__("监控", title="监控", template=False, quit_button=None)
        self._config = config
        self._holdings_config = config.holdings
        self._indices_config: List[IndexConfig] = config.indices
        # Build sources list based on config
        sources = []
        ds_config = config.data_sources
        if "tencent" in ds_config.enabled:
            sources.append(TencentSource(api_template=config.tencent_api_template))
        if "tdx" in ds_config.enabled and HAS_MOOTDX:
            # mootdx 0.11+ 不返回中文名，由 holdings 提供映射
            name_map = {h.code: h.name for h in config.holdings}
            name_map.update({idx.code: idx.name for idx in config.indices})
            sources.append(TDXSource(name_map=name_map))
        # Fallback to Tencent if no sources configured
        if not sources:
            sources.append(TencentSource(api_template=config.tencent_api_template))
        self._fetcher = RotatingMultiFetcher(
            sources,
            successive_fail_limit=ds_config.successive_fail_limit,
            cooldown_seconds=ds_config.cooldown_seconds,
        )
        self._monitor = PriceMonitor(
            alert_threshold_pct=config.alert_threshold_pct,
            sudden_threshold_pct=config.sudden_threshold_pct,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            db_path=config.db_path,
        )
        self._latest_quotes: Dict[str, StockQuote] = {}
        self._quotes_lock = threading.Lock()
        self._menu_items: Dict[str, rumps.MenuItem] = {}
        self._detail_items: Dict[str, List[rumps.MenuItem]] = {}
        self._index_items: Dict[str, rumps.MenuItem] = {}
        self._portfolio_item: rumps.MenuItem = rumps.MenuItem("持仓: 加载中...")
        self._timestamp_item: rumps.MenuItem = rumps.MenuItem("刷新于: --")
        self._news_item: Optional[rumps.MenuItem] = None
        self._news_detail_items: List[rumps.MenuItem] = []
        self._news_lock = threading.Lock()
        self._running = False
        self._heartbeat_count: int = 0
        self._ui_updater = _UIUpdater.alloc().initWithApp_(self)
        self._build_menu()

        self._db = PriceDB(config.db_path)
        holdings_codes = {h.code for h in config.holdings if h.cost is not None}
        self._news_monitor: Optional[NewsMonitor] = None
        if config.news.enabled:
            try:
                self._news_monitor = NewsMonitor(
                    config=config.news,
                    holdings=holdings_codes,
                    db=self._db,
                    on_update=self._on_news_update,
                )
            except Exception as e:
                logger.warning(f"⚠️ 新闻模块初始化失败: {e}")
                self._news_monitor = None

        self._pending_news: List[dict] = []

        logger.info("📊 监控已启动: %d 只持仓, %d 个指数, 刷新间隔 %ds",
                    len(self._config.holdings),
                    len(self._indices_config),
                    self._config.poll_interval_seconds)

    def _build_menu(self) -> None:
        self.menu.clear()
        self._menu_items.clear()
        self._detail_items.clear()
        self._index_items.clear()

        # Holdings manager submenu
        mgr = rumps.MenuItem("管理持仓 ▶")
        add_item = rumps.MenuItem("➕ 添加")
        edit_item = rumps.MenuItem("✏️ 编辑 ▶")
        del_item = rumps.MenuItem("➖ 删除 ▶")
        mgr.add(add_item)
        mgr.add(edit_item)
        mgr.add(del_item)

        for holding in self._config.holdings:
            edit_sub = rumps.MenuItem(f"{holding.name}({holding.code})")
            edit_sub.set_callback(lambda s, h=holding: self._on_edit_holding(h))
            edit_item.add(edit_sub)

            del_sub = rumps.MenuItem(f"{holding.name}({holding.code})")
            del_sub.set_callback(lambda s, h=holding: self._on_delete_holding(h))
            del_item.add(del_sub)

        add_item.set_callback(lambda s: self._on_add_holding())
        self.menu.add(mgr)
        self.menu.add(rumps.separator)

        self._portfolio_item = rumps.MenuItem("持仓: 加载中...")
        self.menu.add(self._portfolio_item)
        self.menu.add(rumps.separator)

        if self._indices_config:
            for idx in self._indices_config:
                item = rumps.MenuItem(f"{idx.name}: 加载中...")
                self._index_items[idx.code] = item
                self.menu.add(item)
            self.menu.add(rumps.separator)

        for holding in self._config.holdings:
            main_item = rumps.MenuItem(f"{holding.name}({holding.code}): 加载中...")
            self._menu_items[holding.code] = main_item

            details = [
                rumps.MenuItem(f"名称: {holding.name}"),
                rumps.MenuItem(f"代码: {holding.code}"),
                rumps.MenuItem("现价: --"),
                rumps.MenuItem("昨收: --"),
                rumps.MenuItem("涨跌额: --"),
                rumps.MenuItem("涨跌幅: --"),
            ]
            if holding.cost is not None:
                details.append(rumps.MenuItem(f"成本: ¥{holding.cost:.2f}"))
            if holding.shares is not None:
                details.append(rumps.MenuItem(f"持仓: {holding.shares} 股"))
            details.append(rumps.MenuItem("盈亏额: --"))
            details.append(rumps.MenuItem("盈亏率: --"))
            details.append(rumps.MenuItem("今日盈亏: --"))
            details.append(rumps.MenuItem("趋势: --"))

            self._detail_items[holding.code] = details

            for detail in details:
                main_item.add(detail)
            self.menu.add(main_item)

        self.menu.add(rumps.separator)
        self._timestamp_item = rumps.MenuItem("刷新于: --")
        self.menu.add(self._timestamp_item)

        if self._config.news.enabled:
            self._news_item = rumps.MenuItem("📰 新闻分析 (暂无)")
            self.menu.add(self._news_item)
            placeholder = rumps.MenuItem("  (等待新分析…)")
            self._news_detail_items.append(placeholder)
            self._news_item.add(placeholder)

            if self._config.news.digest.enabled:
                self._digest_item = rumps.MenuItem("📊 每日精选 (加载中…)")
                self.menu.add(self._digest_item)

        self.menu.add(rumps.separator)

        quit_item = rumps.MenuItem("退出")
        quit_item.set_callback(self._on_quit)
        self.menu.add(quit_item)

    def _on_quit(self, _sender) -> None:
        self._running = False
        rumps.quit_application()

    def _show_holding_dialog(self, mode, holding=None):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"{'编辑' if mode == 'edit' else '添加'}持仓")
        alert.addButtonWithTitle_("确定")
        alert.addButtonWithTitle_("取消")

        content_view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 400, 200))

        labels = ["代码", "名称", "成本", "股数", "止损(%)", "止盈(%)"]
        defaults = ["", "", "", "", "", ""]
        if holding:
            defaults = [holding.code, holding.name,
                        str(holding.cost) if holding.cost else "",
                        str(holding.shares) if holding.shares else "",
                        str(holding.stop_loss) if holding.stop_loss else "",
                        str(holding.take_profit) if holding.take_profit else ""]
        if mode == 'edit':
            defaults[0] = holding.code

        fields = []
        for i, (label, default_val) in enumerate(zip(labels, defaults)):
            y = 170 - i * 28
            lbl = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, y, 80, 20))
            lbl.setStringValue_(label)
            lbl.setEditable_(False)
            lbl.setBordered_(False)
            content_view.addSubview_(lbl)

            field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(90, y, 300, 22))
            field.setStringValue_(default_val)
            if mode == 'edit' and i == 0:
                field.setEditable_(False)
            content_view.addSubview_(field)
            fields.append(field)

        alert.setAccessoryView_(content_view)
        response = alert.runModal()

        if response == 0:
            values = [f.stringValue() for f in fields]
            code = values[0].strip()
            name = values[1].strip()
            cost = float(values[2]) if values[2] else None
            shares = int(values[3]) if values[3] else None
            stop_loss = float(values[4]) if values[4] else None
            take_profit = float(values[5]) if values[5] else None
            return HoldingConfig(code=code, name=name, cost=cost, shares=shares,
                                 stop_loss=stop_loss, take_profit=take_profit)
        return None

    def _on_add_holding(self, _sender=None):
        new_holding = self._show_holding_dialog('add')
        if new_holding:
            self._config.holdings.append(new_holding)
            self._rebuild_and_refresh()

    def _on_edit_holding(self, holding, _sender=None):
        updated = self._show_holding_dialog('edit', holding)
        if updated:
            holding.name = updated.name
            holding.cost = updated.cost
            holding.shares = updated.shares
            holding.stop_loss = updated.stop_loss
            holding.take_profit = updated.take_profit
            self._rebuild_and_refresh()

    def _on_delete_holding(self, holding, _sender=None):
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"确认删除 {holding.name}？")
        alert.setInformativeText_(f"股票代码: {holding.code}")
        alert.addButtonWithTitle_("确认")
        alert.addButtonWithTitle_("取消")
        response = alert.runModal()
        if response == 0:
            self._config.holdings.remove(holding)
            self._rebuild_and_refresh()

    def _rebuild_and_refresh(self):
        save_config(self._config)
        self._build_menu()
        self._do_fetch()

    def _do_fetch(self) -> None:
        codes = [h.code for h in self._config.holdings]
        if self._indices_config:
            codes.extend([idx.code for idx in self._indices_config])
        try:
            quotes = self._fetcher.fetch(codes)
            with self._quotes_lock:
                for quote in quotes:
                    self._latest_quotes[quote.code] = quote
            self._monitor.save_history(quotes)
        except Exception as e:
            logger.error(f"Fetch error: {e}")

    def _on_news_update(self, analysis, related, hits_holdings) -> None:
        """Callback from NewsMonitor (background thread)."""
        from app.news.models import Stock
        stock_pairs = [
            (s.code, s.name) for s in analysis.stocks
        ]  # preserve order
        with self._news_lock:
            self._pending_news.append({
                "summary": analysis.summary,
                "direction": analysis.direction,
                "direction_label": analysis.direction_label,
                "emoji": analysis.emoji,
                "category_emoji": analysis.category_emoji,
                "badge": analysis.badge,
                "is_kneck": analysis.is_kneck,
                "kness_pillars": analysis.kness_pillars_label,
                "scarcity_pillars": list(analysis.scarcity_pillars),
                "narrative_themes": list(analysis.narrative_themes),
                "industry_certainty": analysis.industry_certainty,
                "trend_horizon_years": analysis.trend_horizon_years,
                "confidence": analysis.confidence,
                "sectors": list(analysis.sectors),
                "stock_pairs": stock_pairs,   # [(code, name), ...]
                "news_category": analysis.news_category,
                "bottleneck_order_signal": analysis.bottleneck_order_signal,
                "bottleneck_capacity_signal": analysis.bottleneck_capacity_signal,
                "bottleneck_margin_signal": analysis.bottleneck_margin_signal,
                "rationale": analysis.rationale,
                "related": list(related),
                "hits_holdings": bool(hits_holdings),
                "analyzed_at": analysis.analyzed_at,
            })
            self._pending_news = self._pending_news[:20]

    @staticmethod
    def _format_stock(code: str, name: str) -> str:
        """Format stock for display: 'name(code)' or fallback to code/name."""
        if name and code:
            return f"{name}({code})"
        return name or code

    @staticmethod
    def _lookup_name(code: str, stock_pairs: list) -> str:
        """Find name for code from [(code, name), ...] pairs."""
        for c, n in stock_pairs:
            if c == code and n:
                return n
        return ""

    def _refresh_news_menu(self) -> None:
        """Re-render the news submenu (main thread)."""
        if not self._news_item:
            return
        with self._news_lock:
            items = list(reversed(self._pending_news))

        try:
            self._news_item.title = f"📰 新闻分析 ({len(items)})"
            submenu = self._news_item._menuitem.submenu()
            for child in list(submenu.itemArray() or []):
                submenu.removeItem_(child)
            self._news_detail_items = []
        except Exception as e:
            logger.debug(f"news menu rebuild failed: {e}")
            return

        if not items:
            placeholder = rumps.MenuItem("  (等待新分析…)")
            self._news_item.add(placeholder)
            self._news_detail_items.append(placeholder)
            return

        holdings_codes = {h.code for h in self._config.holdings}
        for entry in items[:10]:
            stars = "🔔" if entry["hits_holdings"] else ("⭐" if entry["confidence"] >= 0.85 else "·")
            cat = entry.get("category_emoji", "📰")
            badge = entry.get("badge", "")
            badge_part = f" {badge}" if badge else ""
            ts = datetime.fromtimestamp(entry["analyzed_at"]).strftime("%H:%M")
            head = rumps.MenuItem(
                f"{stars} {entry['emoji']} {cat} [{','.join(entry['sectors'][:2])}] {entry['summary'][:30]}{badge_part}"
            )
            head.add(rumps.MenuItem(f"方向: {entry['direction_label']}  置信度 {entry['confidence']:.2f}"))
            if entry.get("news_category"):
                head.add(rumps.MenuItem(f"类型: {entry['news_category']}"))
            if entry.get("is_kneck") and entry.get("kness_pillars"):
                head.add(rumps.MenuItem(f"🔧 卡脖子: {entry['kness_pillars']}"))
            if entry.get("narrative_themes"):
                head.add(rumps.MenuItem(f"主题: {', '.join(entry['narrative_themes'][:3])}"))
            three = entry.get("bottleneck_order_signal", "none")
            capacity = entry.get("bottleneck_capacity_signal", "none")
            margin = entry.get("bottleneck_margin_signal", "unknown")
            if three != "none" or capacity != "none" or margin != "unknown":
                head.add(rumps.MenuItem(f"瓶颈: 订单={three}  产能={capacity}  毛利={margin}"))
            if entry.get("industry_certainty") and entry.get("industry_certainty") != "speculative":
                head.add(rumps.MenuItem(
                    f"趋势: {entry['industry_certainty']} / {entry.get('trend_horizon_years', 1)}年"
                ))
            if entry.get("rationale"):
                head.add(rumps.MenuItem(f"理由: {entry['rationale'][:60]}"))
            if entry["hits_holdings"]:
                hit_codes = [c for c in entry["related"] if c in holdings_codes]
                hit_names = [
                    self._format_stock(c, self._lookup_name(c, entry.get("stock_pairs", [])))
                    for c in hit_codes
                ]
                head.add(rumps.MenuItem(f"🔔 命中持仓: {', '.join(hit_names)}"))
            elif entry["related"]:
                related_strs = [
                    self._format_stock(c, self._lookup_name(c, entry.get("stock_pairs", [])))
                    for c in entry["related"][:6]
                ]
                head.add(rumps.MenuItem(f"相关股: {', '.join(related_strs)}"))
            head.add(rumps.MenuItem(f"⏱ {ts}"))
            self._news_item.add(head)
            self._news_detail_items.append(head)

        self._refresh_digest_menu()

    def _refresh_digest_menu(self) -> None:
        """Render recent digests in the 📊 每日精选 submenu."""
        if not self._digest_item:
            return
        try:
            digests = self._db.news_get_recent_digests(limit=7)
            self._digest_item.title = f"📊 每日精选 ({len(digests)})"
            submenu = self._digest_item._menuitem.submenu()
            for child in list(submenu.itemArray() or []):
                submenu.removeItem_(child)
            self._digest_detail_items = []

            if not digests:
                submenu.addItem_(rumps.MenuItem("  (等待首次 digest 分析…)")._menuitem)
                return

            for d in digests:
                sentiment_emoji = {
                    "bullish": "🟢", "bearish": "🔴",
                    "neutral": "⚪", "volatile": "🟡",
                }.get(d["sentiment"], "⚪")
                head = rumps.MenuItem(
                    f"{sentiment_emoji} [{d['date_range']}] "
                    f"{d['sentiment']}({d['confidence']:.2f})"
                )

                head.add(rumps.MenuItem(f"摘要: {d['summary'][:120]}"))
                if d.get("rationale"):
                    head.add(rumps.MenuItem(f"推理: {d['rationale'][:120]}"))

                if d.get("narrative_themes"):
                    head.add(rumps.MenuItem(
                        f"主题: {', '.join(d['narrative_themes'][:5])}"
                    ))

                holdings = d.get("holdings_impacts", []) or []
                if holdings:
                    lines = []
                    for h in holdings[:5]:
                        if not isinstance(h, dict):
                            continue
                        code = h.get("code", "")
                        name = h.get("name", "")
                        impact = h.get("impact", "")
                        conf = h.get("confidence", 0)
                        reason = h.get("reason", "")
                        icon = "🟢" if impact == "positive" else (
                            "🔴" if impact == "negative" else "⚪")
                        disp = (name + f"({code})") if name else code
                        lines.append(f"{icon} {disp} ({conf:.2f}): {reason[:40]}")
                    head.add(rumps.MenuItem(f"持仓影响: {' / '.join(lines)}"))

                sectors = d.get("sector_impacts", []) or []
                if sectors:
                    lines = []
                    for s in sectors[:4]:
                        if not isinstance(s, dict):
                            continue
                        direction_emoji = {"bullish": "🟢", "bearish": "🔴"}.get(
                            s.get("direction", ""), "⚪")
                        lines.append(f"{direction_emoji} {s.get('sector','')}: {s.get('reason','')[:50]}")
                    head.add(rumps.MenuItem(f"板块: {' / '.join(lines)}"))

                key_events = d.get("key_events", []) or []
                if key_events:
                    head.add(rumps.MenuItem(f"要闻: {' | '.join(key_events[:3])}"))

                ts = datetime.fromtimestamp(d["analyzed_at"]).strftime("%m-%d %H:%M")
                head.add(rumps.MenuItem(f"⏱ {ts}"))
                self._digest_item._menuitem.submenu().addItem_(head._menuitem)
                self._digest_detail_items.append(head)
        except Exception as e:
            logger.debug(f"digest menu rebuild failed: {e}")

    def _update_ui(self) -> None:
        with self._quotes_lock:
            quotes = list(self._latest_quotes.values())

        if not quotes:
            return

        quote_map = {q.code: q for q in quotes}

        alerts = self._monitor.update(quotes)
        for alert in alerts:
            title = f"{alert.stock_name} {alert.direction}"
            subtitle = f"{alert.change_pct:+.2f}%"
            message = f"现价: ¥{alert.current_price}"
            rumps.notification(title=title, subtitle=subtitle, message=message)

        holdings_with_cost = [h for h in self._holdings_config if h.cost is not None]
        pnl_list, risk_alerts = self._monitor.update_holdings(quotes, holdings_with_cost)

        consecutive_alerts = self._monitor.check_consecutive(quotes)
        risk_alerts.extend(consecutive_alerts)

        trend_alerts = self._monitor.check_trend(quotes)
        risk_alerts.extend(trend_alerts)

        acceleration_alerts = self._monitor.check_acceleration(quotes)
        risk_alerts.extend(acceleration_alerts)

        for alert in risk_alerts:
            emoji = "🚨" if alert.alert_type == "止损" else "🎯" if alert.alert_type == "止盈" else "📊"
            title = f"{emoji} {alert.alert_type}: {alert.stock_name}"
            subtitle = f"{alert.pnl_pct:+.1f}%"
            message = alert.message
            rumps.notification(title=title, subtitle=subtitle, message=message)

        total_pnl = sum(p.pnl_amount for p in pnl_list)
        total_cost = sum(p.cost * p.shares for p in pnl_list)
        total_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
        total_daily_pnl = sum(p.daily_pnl_amount for p in pnl_list)
        sign = "+" if total_pnl >= 0 else ""
        daily_sign = "+" if total_daily_pnl >= 0 else ""
        portfolio_title = f"持仓: {sign}¥{total_pnl:.0f} ({sign}{total_pct:.1f}%)  |  今日: {daily_sign}¥{total_daily_pnl:.0f}"
        if not is_market_open():
            portfolio_title += "  🕐 已收盘"
        portfolio_color = COLOR_UP if total_pnl >= 0 else COLOR_DOWN
        attr_str = AppKit.NSMutableAttributedString.alloc().initWithString_(portfolio_title)
        if not is_market_open():
            closed_len = len("  🕐 已收盘")
            attr_str.addAttributes_range_(
                {
                    AppKit.NSForegroundColorAttributeName: COLOR_FLAT,
                    AppKit.NSFontAttributeName: FONT,
                },
                (len(portfolio_title) - closed_len, closed_len),
            )
            attr_str.addAttributes_range_(
                {
                    AppKit.NSForegroundColorAttributeName: portfolio_color,
                    AppKit.NSFontAttributeName: FONT,
                },
                (0, len(portfolio_title) - closed_len),
            )
        else:
            attr_str.addAttributes_range_(
                {
                    AppKit.NSForegroundColorAttributeName: portfolio_color,
                    AppKit.NSFontAttributeName: FONT,
                },
                (0, len(portfolio_title)),
            )
        self._portfolio_item._menuitem.setAttributedTitle_(attr_str)

        pnl_map = {p.code: p for p in pnl_list}
        risk_map = {r.code: r for r in risk_alerts}

        up_count = 0
        down_count = 0

        for quote in quotes:
            main_item = self._menu_items.get(quote.code)
            details = self._detail_items.get(quote.code)
            if main_item is None or details is None:
                continue

            color = _color_for_change_pct(quote.change_pct)
            pnl = pnl_map.get(quote.code)
            risk = risk_map.get(quote.code)

            sign = "+" if quote.change_pct >= 0 else ""
            if pnl is not None:
                pnl_sign = "+" if pnl.pnl_pct >= 0 else ""
                risk_marker = ""
                if risk is not None:
                    risk_marker = " 🚨" if risk.alert_type == "止损" else " 🎯" if risk.alert_type == "止盈" else ""
                title = f"{quote.name}({quote.code}): ¥{quote.current_price:.2f}  {sign}{quote.change_pct:.2f}%  |  {pnl_sign}{pnl.pnl_pct:.1f}%{risk_marker}"
            else:
                title = f"{quote.name}({quote.code}): ¥{quote.current_price:.2f}  {sign}{quote.change_pct:.2f}%"
            _set_attributed_title(main_item, title, color)

            if len(details) >= 6:
                details[2].title = f"现价: ¥{quote.current_price:.2f}"
                details[3].title = f"昨收: ¥{quote.yesterday_close:.2f}"
                sign_amount = "+" if quote.change_amount >= 0 else ""
                details[4].title = f"涨跌额: {sign_amount}{quote.change_amount:.2f}"
                sign_pct = "+" if quote.change_pct >= 0 else ""
                details[5].title = f"涨跌幅: {sign_pct}{quote.change_pct:.2f}%"

            trend = self._monitor.get_trend(quote.code)
            for detail in details:
                if detail.title.startswith("盈亏额:"):
                    if pnl is not None:
                        pnl_sign = "+" if pnl.pnl_amount >= 0 else ""
                        detail.title = f"盈亏额: {pnl_sign}¥{pnl.pnl_amount:.0f}"
                elif detail.title.startswith("盈亏率:"):
                    if pnl is not None:
                        pnl_sign = "+" if pnl.pnl_pct >= 0 else ""
                        detail.title = f"盈亏率: {pnl_sign}{pnl.pnl_pct:.1f}%"
                elif detail.title.startswith("今日盈亏:"):
                    if pnl is not None:
                        d_sign = "+" if pnl.daily_pnl_amount >= 0 else ""
                        detail.title = f"今日盈亏: {d_sign}¥{pnl.daily_pnl_amount:.0f} ({d_sign}{pnl.daily_pnl_pct:.1f}%)"
                elif detail.title.startswith("趋势:"):
                    detail.title = f"趋势: {trend}"

            if quote.change_pct > 0:
                up_count += 1
            elif quote.change_pct < 0:
                down_count += 1

        for idx_code, item in self._index_items.items():
            quote = quote_map.get(idx_code)
            if quote:
                color = _color_for_change_pct(quote.change_pct)
                idx_sign = "+" if quote.change_pct >= 0 else ""
                _set_attributed_title(item, f"{quote.name}: {quote.current_price:.0f}  {idx_sign}{quote.change_pct:.2f}%", color)

        market_indicator = "" if is_market_open() else " 🕐"
        self.title = f"{market_indicator} | {up_count}↑ {down_count}↓"

        now = datetime.now().strftime("%H:%M:%S")
        self._timestamp_item.title = f"刷新于: {now}"

        self._refresh_news_menu()

    def _refresh_loop(self) -> None:
        while self._running:
            self._do_fetch()
            self._heartbeat_count += 1
            if 1 <= self._heartbeat_count <= 5 or self._heartbeat_count % 60 == 0:
                elapsed_minutes = self._heartbeat_count * self._config.poll_interval_seconds // 60
                logger.info("✓ 第 %d 轮刷新完成 (约 %d 分钟)", self._heartbeat_count, elapsed_minutes)
            self._ui_updater.performSelectorOnMainThread_withObject_waitUntilDone_(
                self._ui_updater.updateUI_, None, False
            )
            if is_market_open():
                time.sleep(self._config.poll_interval_seconds)
            else:
                time.sleep(60)

    def run(self) -> None:
        self._do_fetch()
        self._update_ui()
        self._running = True
        threading.Thread(target=self._refresh_loop, daemon=True).start()
        if self._news_monitor:
            self._news_monitor.start()
            health = self._news_monitor.health_check()
            logger.info(
                "📰 新闻模块: cls=%s llm=%s sector=%s",
                health["cls"], health["llm"], health["sector"],
            )
        super().run()
