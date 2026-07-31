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
        holdings_config: Optional[list] = None,
    ):
        self._config = config
        self._holdings = holdings
        self._holdings_config = holdings_config or []
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

        self._consecutive_llm_failures: int = 0
        self._llm_circuit_open: bool = False
        self._llm_circuit_open_until: float = 0.0

        self._tick_count: int = 0
        self._last_status_log_ts: float = time.time()

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
        logger.info(
            "[NewsMonitor] starting... health=cls:%s llm:%s sector:%s | "
            "config: poll=%ds/%ds (intraday/off-hours) daily_limit=%d "
            "filter: kw=%.2f notif_conf=%.2f | "
            "llm: model=%s timeout=%ds max/min=%d",
            *self.health_check().values(),
            self._config.cls.poll_interval_seconds,
            self._config.cls.off_hours_poll_interval_seconds,
            self._config.llm.daily_limit,
            self._config.filter.keyword_threshold,
            self._config.filter.min_confidence_for_notify,
            self._analyzer._model,
            self._analyzer._timeout,
            self._config.llm.max_per_minute,
        )
        if self._digest_fetcher and self._digest_analyzer:
            logger.info(
                "[NewsMonitor] digest enabled: every %d min, days_back=%d, "
                "min_market_conf=%.2f, min_holdings_conf=%.2f",
                self._config.digest.poll_interval_minutes,
                self._config.digest.days_back,
                self._config.digest.min_market_confidence_for_notify,
                self._config.digest.min_holdings_confidence_for_notify,
            )
        threading.Thread(target=self._loop, daemon=True, name="NewsMonitor").start()
        if self._digest_fetcher and self._digest_analyzer:
            threading.Thread(
                target=self._digest_loop,
                daemon=True,
                name="NewsMonitor-Digest",
            ).start()
        logger.info("[NewsMonitor] started (NewsMonitor + %s threads running)",
                    "NewsMonitor-Digest" if self._digest_fetcher else "no digest")

    def stop(self) -> None:
        self._running = False

    def _current_interval(self) -> int:
        return (
            self._config.cls.poll_interval_seconds
            if is_market_open()
            else self._config.cls.off_hours_poll_interval_seconds
        )

    def _maybe_log_status(self) -> None:
        """Periodic status summary: every 5 min, or every 100 ticks."""
        now = time.time()
        if (now - self._last_status_log_ts) < 300 and self._tick_count % 100 != 0:
            return
        self._last_status_log_ts = now
        circuit = "OPEN" if self._llm_circuit_open else "closed"
        quota_limit = self._config.llm.daily_limit
        remaining = max(0, quota_limit - self._daily_count) if quota_limit > 0 else "∞"
        logger.info(
            f"[Status] ticks={self._tick_count} digest_runs=0 "
            f"quota={self._daily_count}/{quota_limit}(剩{remaining}) "
            f"notif={self._notif_count} llm_fails_streak={self._consecutive_llm_failures} "
            f"circuit={circuit} market={'🟢open' if is_market_open() else '🌙closed'}"
        )

    def _normalize_holdings(self, items: list) -> list:
        """Override LLM-hallucinated names with config-grounded names.

        LLM sometimes returns wrong code-name pairs (e.g. sh601949
        labeled as 中国石化, which is actually sh600028). This helper
        forces names to match the user's known holdings.
        """
        if not items:
            return items
        names_by_code = {h.code: h.name for h in self._holdings_config}
        for item in items:
            if not isinstance(item, dict):
                continue
            code = item.get("code", "")
            if code in names_by_code:
                item["name"] = names_by_code[code]
        return items

    def _record_llm_failure(self) -> None:
        """Track consecutive LLM failures; open circuit after 3 in a row."""
        self._consecutive_llm_failures += 1
        if self._consecutive_llm_failures >= 3:
            cooldown = 5 * 60
            self._llm_circuit_open = True
            self._llm_circuit_open_until = time.time() + cooldown
            logger.warning(
                f"[NewsMonitor] LLM circuit OPEN after {self._consecutive_llm_failures} failures, "
                f"cooldown 5 min"
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
        self._tick_count += 1
        raw_news = self._fetcher.fetch(max_items=30)
        if not raw_news:
            self._maybe_log_status()
            return

        stats = {"fetched": len(raw_news), "dup": 0, "kw_skip": 0,
                 "llm_ok": 0, "llm_fail": 0, "noise": 0, "saved": 0, "notif": 0,
                 "circuit_skip": 0, "quota_skip": 0}

        for news in raw_news:
            seen = self._db.news_seen(news.hash, news.title, news.content, news.ctime)
            if seen:
                stats["dup"] += 1
                continue

            score = keyword_score(news.title, news.content)
            if score < self._config.filter.keyword_threshold:
                stats["kw_skip"] += 1
                logger.debug(
                    f"[NewsMonitor] skip (low keyword score {score:.2f}): {news.title[:30]}"
                )
                continue

            self._bucket.acquire()
            if not self._check_daily_limit():
                stats["quota_skip"] += 1
                logger.debug(f"[NewsMonitor] daily limit hit, skipping LLM for {news.hash}")
                continue
            if self._llm_circuit_open:
                if time.time() < self._llm_circuit_open_until:
                    stats["circuit_skip"] += 1
                    logger.debug(
                        f"[NewsMonitor] LLM circuit open, skip {news.hash}"
                    )
                    continue
                self._llm_circuit_open = False
                logger.info("[NewsMonitor] LLM circuit half-open, retrying")
            analysis = self._analyzer.analyze(news)
            if analysis is None:
                self._record_llm_failure()
                stats["llm_fail"] += 1
                logger.debug(f"[NewsMonitor] LLM failed for {news.hash}")
                continue
            self._consecutive_llm_failures = 0
            stats["llm_ok"] += 1
            self._daily_count += 1

            if analysis.confidence < self._config.filter.min_save_confidence:
                stats["noise"] += 1
                logger.debug(
                    f"[NewsMonitor] pure noise (conf {analysis.confidence:.2f}), skipping save"
                )
                continue

            related = self._sector_mapper.map_analysis(analysis.sectors, analysis.stocks)
            related_codes = {s.code for s in related}
            hits_holdings: bool = bool(self._holdings & related_codes)

            # Override LLM-hallucinated names in news's own stocks list
            self._normalize_holdings(
                [{"code": s.code, "name": s.name} for s in analysis.stocks]
            )
            # Also normalize holdings impacts if any leaked in (defensive)

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
            stats["saved"] += 1

            if self._should_notify(analysis, hits_holdings):
                if self._notify(analysis, related, hits_holdings):
                    self._db.news_mark_notified(analysis.news_hash)
                    self._notif_count += 1
                    stats["notif"] += 1

            if self._on_update:
                try:
                    self._on_update(analysis, related, hits_holdings)
                except Exception as e:
                    logger.debug(f"[NewsMonitor] on_update callback error: {e}")

        market = "🟢" if is_market_open() else "🌙"
        logger.info(
            f"[Tick #{self._tick_count}] {market} "
            f"CLS→{stats['fetched']} dup={stats['dup']} kw❌={stats['kw_skip']} "
            f"→ LLM={stats['llm_ok']}✓/{stats['llm_fail']}✗ "
            f"(noise={stats['noise']} circuit={stats['circuit_skip']} quota={stats['quota_skip']}) "
            f"→ saved={stats['saved']} notif={stats['notif']} | "
            f"quota={self._daily_count}/{self._config.llm.daily_limit} "
            f"fail_streak={self._consecutive_llm_failures}"
        )
        self._maybe_log_status()

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
        import time as time_mod
        t_start = time_mod.time()
        if not self._digest_fetcher or not self._digest_analyzer:
            logger.debug("[NewsMonitor] digest not configured, skipping")
            return None

        logger.info("[Digest] starting: fetching from CLS...")
        digests = self._digest_fetcher.fetch()
        if not digests:
            logger.info("[Digest] no digests returned (off-hours or API issue)")
            return None
        logger.info(
            f"[Digest] fetched {len(digests)} digests, filtering to last {self._config.digest.days_back} day(s)..."
        )

        cfg = self._config.digest
        cutoff = time_mod.time() - cfg.days_back * 86400
        recent = [d for d in digests if d.ctime >= cutoff]
        if not recent:
            logger.info(
                f"[Digest] no digests within last {cfg.days_back} day(s) (latest was {digests[0].digest_date})"
            )
            return None
        logger.info(
            f"[Digest] {len(recent)} digests in window, types: "
            f"{', '.join(f'{d.digest_type}({d.digest_date})' for d in recent)}"
        )

        # Sort by date asc (oldest first) so LLM reads chronologically
        recent.sort(key=lambda d: d.ctime)

        self._bucket.acquire()
        if not self._check_daily_limit():
            logger.info("[Digest] daily LLM limit hit, skipping cycle")
            return None

        logger.info(
            f"[Digest] calling LLM ({self._analyzer._model}) for {len(recent)} digests, "
            f"{self._holdings and len(self._holdings) or 0} holdings..."
        )
        holdings = [Stock(code=c, name="") for c in self._holdings]
        analysis = self._digest_analyzer.analyze(recent, holdings)
        llm_elapsed = time_mod.time() - t_start
        if analysis is None:
            self._record_llm_failure()
            logger.warning(f"[Digest] LLM returned None after {llm_elapsed:.0f}s")
            return None
        self._consecutive_llm_failures = 0
        self._daily_count += 1

        logger.info(
            f"[Digest] ✓ LLM done in {llm_elapsed:.0f}s: "
            f"market={analysis.market_sentiment}({analysis.market_confidence:.2f}) "
            f"sectors={len(analysis.sector_impacts)} "
            f"holdings_impact={len(analysis.holdings_impacts)} "
            f"themes={analysis.narrative_themes[:3]}"
        )
        logger.info(
            f"[Digest] summary: {analysis.summary[:200]}"
        )

        # Persist to DB for menu/UI display and historical comparison
        # Override LLM-hallucinated stock names with config-grounded names
        self._normalize_holdings(analysis.holdings_impacts)
        self._db.news_save_digest({
            "analyzed_at": time_mod.time(),
            "date_range": (f"{recent[0].digest_date} ~ {recent[-1].digest_date}"
                           if len(recent) > 1 else recent[0].digest_date),
            "sentiment": analysis.market_sentiment,
            "confidence": analysis.market_confidence,
            "summary": analysis.summary,
            "rationale": analysis.rationale,
            "sector_impacts": analysis.sector_impacts,
            "holdings_impacts": analysis.holdings_impacts,
            "key_events": analysis.key_events,
            "narrative_themes": analysis.narrative_themes,
            "digest_count": len(recent),
            "digest_hashes": analysis.digest_hashes,
        })

        if self._should_notify_digest(analysis):
            logger.info("[Digest] 🔔 notification: market signal meets threshold")
            self._notify_digest(analysis, recent)
        else:
            logger.info(
                f"[Digest] no notification: conf={analysis.market_confidence:.2f} "
                f"sentiment={analysis.market_sentiment}"
            )
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