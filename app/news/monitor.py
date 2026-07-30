"""News monitor: orchestrates fetch → filter → analyze → store → notify."""
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Set

import rumps

from app.config import NewsConfig
from app.monitor import is_market_open
from app.news.analyzer import OllamaAnalyzer, TokenBucket, keyword_score
from app.news.digest import Digest, DigestAnalysis, DigestAnalyzer, DigestFetcher
from app.news.fetcher import ClsFetcher
from app.news.models import NewsAnalysis, Stock
from app.news.sector import SectorMapper
from app.storage import PriceDB

logger = logging.getLogger(__name__)


class NewsMonitor:
    """Background loop that drives the news intelligence pipeline."""

    def __init__(
        self,
        config: NewsConfig,
        holdings: Set[str],
        db: PriceDB,
        on_update=None,
    ):
        self._config = config
        self._holdings = holdings
        self._db = db
        self._on_update = on_update

        self._fetcher = ClsFetcher(
            sign=config.cls.sign,
            cookie=config.cls.cookie,
        )
        self._analyzer = OllamaAnalyzer(
            model=config.llm.model,
            host=config.llm.host,
            api_key=config.llm.api_key,
            timeout=config.llm.request_timeout_seconds,
        )
        self._sector_mapper = SectorMapper()
        self._bucket = TokenBucket(rate_per_minute=config.llm.max_per_minute)

        self._digest_fetcher = None
        self._digest_analyzer = None
        self._digest_running = False
        if config.digest.enabled:
            try:
                self._digest_fetcher = DigestFetcher(
                    sign=config.digest.sign,
                    cookie=config.digest.cookie,
                )
                self._digest_analyzer = DigestAnalyzer(self._analyzer)
            except Exception as e:
                logger.warning(f"⚠️ Digest 模块初始化失败: {e}")

        self._running = False
        self._llm_available = True
        self._last_poll_ts: float = 0.0

        self._daily_count: int = 0
        self._daily_count_date: date = date.today()
        self._daily_limit_warned: bool = False

        self._notif_count: int = 0
        self._notif_count_date: date = date.today()

    def health_check(self) -> dict:
        """Return status of each component."""
        cls_ok = True
        try:
            self._fetcher.fetch(max_items=1)
        except Exception:
            cls_ok = False
        llm_ok = self._analyzer.health_check() if self._llm_available else False
        sector_ok = bool(self._sector_mapper._sector_to_stocks)
        return {"cls": cls_ok, "llm": llm_ok, "sector": sector_ok}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="NewsMonitor").start()
        if self._digest_fetcher and self._digest_analyzer:
            threading.Thread(
                target=self._digest_loop,
                daemon=True,
                name="NewsMonitor-Digest",
            ).start()
            interval = self._config.digest.poll_interval_minutes
            logger.info(
                "[NewsMonitor] digest cycle started (every %d min, days_back=%d)",
                interval, self._config.digest.days_back,
            )
        logger.info("[NewsMonitor] started")

    def stop(self) -> None:
        self._running = False

    def _current_interval(self) -> int:
        return (
            self._config.cls.poll_interval_seconds
            if is_market_open()
            else self._config.cls.off_hours_poll_interval_seconds
        )

    def _check_daily_limit(self) -> bool:
        """Reset counter on date change; warn at 90%; refuse at 100%."""
        limit = self._config.llm.daily_limit
        if limit <= 0:
            return True

        today = date.today()
        if today != self._daily_count_date:
            self._daily_count = 0
            self._daily_count_date = today
            self._daily_limit_warned = False

        if self._daily_count >= limit:
            if not self._daily_limit_warned:
                logger.warning(
                    "[NewsMonitor] daily LLM limit reached (%d/%d), pausing LLM until tomorrow",
                    self._daily_count, limit,
                )
                self._daily_limit_warned = True
            return False

        if self._daily_count >= limit * 0.9 and not self._daily_limit_warned:
            logger.warning(
                "[NewsMonitor] approaching daily LLM limit (%d/%d, %.0f%%)",
                self._daily_count, limit,
                100 * self._daily_count / limit,
            )
            self._daily_limit_warned = True

        return True

    @property
    def daily_usage(self) -> dict:
        """For UI / debug: current LLM usage stats."""
        limit = self._config.llm.daily_limit
        return {
            "date": self._daily_count_date.isoformat(),
            "count": self._daily_count,
            "limit": limit,
            "remaining": max(0, limit - self._daily_count) if limit > 0 else None,
        }

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.exception(f"[NewsMonitor] tick failed: {e}")
            time.sleep(self._current_interval())

    def _digest_loop(self) -> None:
        """Background loop: run_digest_cycle() every N minutes during market hours.

        Off-hours: sleep 30 min between attempts (cheap check, no LLM cost since
        run_digest_cycle short-circuits on no data or off-hours digests).
        """
        while self._running:
            try:
                self.run_digest_cycle()
            except Exception as e:
                logger.exception(f"[NewsMonitor] digest cycle failed: {e}")
            interval = self._config.digest.poll_interval_minutes
            sleep_sec = interval * 60 if is_market_open() else 1800
            time.sleep(sleep_sec)

    def _tick(self) -> None:
        raw_news = self._fetcher.fetch(max_items=30)
        if not raw_news:
            return

        new_count = 0
        for news in raw_news:
            seen = self._db.news_seen(news.hash, news.title, news.content, news.ctime)
            if seen:
                continue

            score = keyword_score(news.title, news.content)
            if score < self._config.filter.keyword_threshold:
                logger.debug(
                    f"[NewsMonitor] skip (low keyword score {score:.2f}): {news.title[:30]}"
                )
                continue

            self._bucket.acquire()
            if not self._check_daily_limit():
                logger.debug(f"[NewsMonitor] daily limit hit, skipping LLM for {news.hash}")
                continue
            analysis = self._analyzer.analyze(news)
            self._daily_count += 1
            if analysis is None:
                logger.debug(f"[NewsMonitor] LLM failed for {news.hash}")
                continue

            if analysis.confidence < self._config.filter.min_save_confidence:
                logger.debug(
                    f"[NewsMonitor] pure noise (conf {analysis.confidence:.2f}), skipping save"
                )
                continue

            related = self._sector_mapper.map_analysis(analysis.sectors, analysis.stocks)
            related_codes = {s.code for s in related}
            hits_holdings: bool = bool(self._holdings & related_codes)

            analysis_dict = {
                "news_hash": analysis.news_hash,
                "summary": analysis.summary,
                "sectors": analysis.sectors,
                "stocks": [s.code for s in analysis.stocks],
                "stock_names": [s.name for s in analysis.stocks],
                "direction": analysis.direction,
                "confidence": analysis.confidence,
                "time_horizon": analysis.time_horizon,
                "rationale": analysis.rationale,
                "news_category": analysis.news_category,
                "bottleneck_order_signal": analysis.bottleneck_order_signal,
                "bottleneck_capacity_signal": analysis.bottleneck_capacity_signal,
                "bottleneck_margin_signal": analysis.bottleneck_margin_signal,
                "is_kneck": analysis.is_kneck,
                "scarcity_pillars": analysis.scarcity_pillars,
                "trend_horizon_years": analysis.trend_horizon_years,
                "industry_certainty": analysis.industry_certainty,
                "narrative_themes": analysis.narrative_themes,
                "related": [{"code": s.code, "name": s.name} for s in related],
            }
            self._db.news_save_analysis(analysis_dict)

            if self._should_notify(analysis, hits_holdings):
                if self._notify(analysis, related, hits_holdings):
                    self._db.news_mark_notified(analysis.news_hash)
                    self._notif_count += 1

            new_count += 1
            if self._on_update:
                try:
                    self._on_update(analysis, related, hits_holdings)
                except Exception as e:
                    logger.debug(f"[NewsMonitor] on_update callback error: {e}")

        if new_count:
            logger.info(f"[NewsMonitor] processed {new_count} new analyses")

    def _should_notify(self, analysis: NewsAnalysis, hits_holdings: bool) -> bool:
        """Three-tier notification gating.

        Tier 1 (CRITICAL):    high confidence (≥ notify) AND non-neutral
        Tier 2 (HOLDINGS):    confidence ≥ holdings_alert AND non-neutral AND hits holdings
        Tier 3 (BOTTLENECK):  is_bottleneck_signal AND confidence ≥ floor AND non-neutral

        Plus: per-analysis usefulness floor (min_useful_confidence) — below this
        we treat even directional outputs as noise.

        Plus: daily notification cap (max_notifications_per_day).
        """
        cfg = self._config.filter

        if analysis.confidence < cfg.min_useful_confidence:
            return False

        non_neutral = analysis.direction in ("bullish", "bearish")
        if not non_neutral:
            return False

        if analysis.confidence >= cfg.min_confidence_for_notify:
            logger.debug(f"[NewsMonitor] notify TIER1 (high conf {analysis.confidence:.2f})")
            return True

        if (hits_holdings
                and analysis.confidence >= cfg.min_confidence_for_holdings_alert):
            logger.debug(
                f"[NewsMonitor] notify TIER2 (holdings hit, conf {analysis.confidence:.2f})"
            )
            return True

        if (analysis.is_bottleneck_signal
                and analysis.confidence >= cfg.bottleneck_confidence_floor):
            logger.debug(
                f"[NewsMonitor] notify TIER3 (bottleneck, conf {analysis.confidence:.2f})"
            )
            return True

        return False

    def _notify(
        self,
        analysis: NewsAnalysis,
        related: List[Stock],
        hits_holdings: bool,
    ) -> bool:
        """Emit macOS notification; returns True if sent, False if capped."""
        if self._notif_count >= self._config.filter.max_notifications_per_day:
            logger.debug(
                f"[NewsMonitor] daily notif cap reached (%d), skipping notification",
                self._notif_count,
            )
            return False

        badge = analysis.badge
        title = f"{analysis.category_emoji} {analysis.summary[:40]}"
        subtitle = f"{analysis.direction_label}  置信度 {analysis.confidence:.2f}"
        if badge:
            subtitle += f"  ·  {badge}"
        message = analysis.rationale
        if analysis.is_kneck and analysis.scarcity_pillars:
            message += f"\n\n🔧 卡脖子: {analysis.kness_pillars_label}"
        if analysis.narrative_themes:
            message += f"\n主题: {' / '.join(analysis.narrative_themes[:3])}"
        if analysis.industry_certainty != "speculative":
            message += f"\n确定性: {analysis.industry_certainty}  时长: {analysis.trend_horizon_years}年"
        if hits_holdings:
            hit_codes = {s.code for s in related if self._holdings and s.code in self._holdings}
            hit_pairs = [(s.code, s.name) for s in related if s.code in hit_codes]
            hit_display = ", ".join(
                (n + f"({c})" if n else c) for c, n in hit_pairs
            ) or ", ".join(sorted(hit_codes))
            message += f"\n\n🔔 命中持仓: {hit_display}"
        elif related:
            related_pairs = [(s.code, s.name) for s in related[:5]]
            related_display = ", ".join(
                (n + f"({c})" if n else c) for c, n in related_pairs
            ) or ", ".join(s.code for s in related[:5])
            message += f"\n相关: {related_display}"
        try:
            rumps.notification(title=title, subtitle=subtitle, message=message)
            return True
        except Exception as e:
            logger.debug(f"[NewsMonitor] notification failed: {e}")
            return False

    def run_digest_cycle(self) -> Optional[DigestAnalysis]:
        """Fetch recent digests, run LLM impact analysis, notify on signal.

        Returns the DigestAnalysis (or None on failure) for menu/UI use.
        """
        if not self._digest_fetcher or not self._digest_analyzer:
            logger.debug("[NewsMonitor] digest not configured, skipping")
            return None

        digests = self._digest_fetcher.fetch()
        if not digests:
            return None

        # Filter to last N days
        cfg = self._config.digest
        cutoff = time.time() - cfg.days_back * 86400
        recent = [d for d in digests if d.ctime >= cutoff]
        if not recent:
            return None

        # Sort by date asc (oldest first) so LLM reads chronologically
        recent.sort(key=lambda d: d.ctime)

        # LLM analysis (counts against daily limit too)
        self._bucket.acquire()
        if not self._check_daily_limit():
            return None

        holdings = [Stock(code=c, name="") for c in self._holdings]
        analysis = self._digest_analyzer.analyze(recent, holdings)
        self._daily_count += 1
        if analysis is None:
            return None

        logger.info(
            f"[NewsMonitor] digest analysis: {len(recent)} digests, "
            f"sentiment={analysis.market_sentiment} "
            f"conf={analysis.market_confidence:.2f} "
            f"holdings_impacts={len(analysis.holdings_impacts)}"
        )

        if self._should_notify_digest(analysis):
            self._notify_digest(analysis, recent)
        return analysis

    def _should_notify_digest(self, analysis: DigestAnalysis) -> bool:
        cfg = self._config.digest
        if self._notif_count >= self._config.filter.max_notifications_per_day:
            return False
        if analysis.market_confidence < cfg.min_market_confidence_for_notify:
            return False
        if analysis.has_holdings_impact:
            strongest = analysis.strongest_holdings_impact
            if strongest and strongest.get("confidence", 0) >= cfg.min_holdings_confidence_for_notify:
                return True
        if analysis.market_sentiment in ("bullish", "bearish", "volatile"):
            return True
        return False

    def _notify_digest(self, analysis: DigestAnalysis, digests: List[Digest]) -> None:
        title = f"📊 每日精选 ({digests[0].digest_date} ~ {digests[-1].digest_date})"
        sentiment_emoji = {
            "bullish": "🟢", "bearish": "🔴",
            "neutral": "⚪", "volatile": "🟡",
        }.get(analysis.market_sentiment, "⚪")
        subtitle = f"{sentiment_emoji} {analysis.market_sentiment}  置信度 {analysis.market_confidence:.2f}"
        message = analysis.summary
        if analysis.holdings_impacts:
            lines = []
            for h in analysis.holdings_impacts[:5]:
                if not isinstance(h, dict):
                    continue
                code = h.get("code", "")
                name = h.get("name", "")
                impact = h.get("impact", "")
                conf = h.get("confidence", 0)
                reason = h.get("reason", "")
                icon = "🟢" if impact == "positive" else ("🔴" if impact == "negative" else "⚪")
                disp = (name + f"({code})") if name else code
                lines.append(f"{icon} {disp} ({conf:.2f}): {reason}")
            message += "\n\n持仓影响:\n" + "\n".join(lines)
        if analysis.sector_impacts:
            lines = []
            for s in analysis.sector_impacts[:3]:
                if not isinstance(s, dict):
                    continue
                direction_emoji = {"bullish": "🟢", "bearish": "🔴"}.get(s.get("direction", ""), "⚪")
                lines.append(f"{direction_emoji} {s.get('sector', '')}: {s.get('reason', '')}")
            message += "\n\n板块:\n" + "\n".join(lines)
        if analysis.key_events:
            message += "\n\n要闻:\n" + " | ".join(analysis.key_events[:3])
        try:
            rumps.notification(title=title, subtitle=subtitle, message=message)
            self._notif_count += 1
        except Exception as e:
            logger.debug(f"[NewsMonitor] digest notification failed: {e}")