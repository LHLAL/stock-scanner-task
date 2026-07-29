import json
import os
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DataSourceConfig:
    enabled: List[str] = field(
        default_factory=lambda: ["tencent", "tdx"]
    )
    successive_fail_limit: int = 3
    cooldown_seconds: int = 60


@dataclass
class ClsConfig:
    sign: Optional[str] = None
    cookie: Optional[str] = None
    poll_interval_seconds: int = 30
    off_hours_poll_interval_seconds: int = 300


@dataclass
class NewsFilterConfig:
    keyword_threshold: float = 0.3
    min_confidence_for_notify: float = 0.7
    min_confidence_for_holdings_alert: float = 0.5


@dataclass
class LlmConfig:
    model: str = "minimax-m2.5:cloud"
    host: str = "http://localhost:11434"
    api_key: Optional[str] = None
    max_per_minute: int = 10
    cache_ttl_hours: int = 24
    request_timeout_seconds: int = 30


@dataclass
class SectorConfig:
    cache_ttl_days: int = 7
    force_refresh: bool = False


@dataclass
class NewsConfig:
    enabled: bool = False
    cls: ClsConfig = field(default_factory=ClsConfig)
    filter: NewsFilterConfig = field(default_factory=NewsFilterConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    sector: SectorConfig = field(default_factory=SectorConfig)


@dataclass
class HoldingConfig:
    code: str
    name: str
    cost: Optional[float] = None
    shares: Optional[int] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class IndexConfig:
    code: str
    name: str


@dataclass
class AppConfig:
    holdings: List[HoldingConfig]
    indices: List[IndexConfig] = field(default_factory=list)
    stop_loss_pct: float = -8.0
    take_profit_pct: float = 15.0
    alert_threshold_pct: float = 2.0
    sudden_threshold_pct: float = 1.0
    poll_interval_seconds: int = 5
    tencent_api_template: str = "http://qt.gtimg.cn/q={codes}"
    db_path: str = "price_history.db"
    data_sources: DataSourceConfig = field(default_factory=DataSourceConfig)
    news: NewsConfig = field(default_factory=NewsConfig)


def _get_project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def save_config(config: AppConfig, path: Optional[str] = None) -> None:
    """Serialize AppConfig back to config.json with backup."""
    if path is None:
        path = os.path.join(_get_project_root(), "config.json")

    # Backup original file
    bak_path = path + ".bak"
    import shutil
    shutil.copy2(path, bak_path)

    data = {
        "holdings": [
            {
                "code": h.code,
                "name": h.name,
                "cost": h.cost,
                "shares": h.shares,
                "stop_loss": h.stop_loss,
                "take_profit": h.take_profit,
            }
            for h in config.holdings
        ],
        "indices": [{"code": idx.code, "name": idx.name} for idx in config.indices],
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit_pct": config.take_profit_pct,
        "alert_threshold_pct": config.alert_threshold_pct,
        "sudden_threshold_pct": config.sudden_threshold_pct,
        "poll_interval_seconds": config.poll_interval_seconds,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_config(path: Optional[str] = None) -> AppConfig:
    if path is None:
        path = os.path.join(_get_project_root(), "config.json")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    holdings = []
    for item in data.get("holdings", []):
        code = item["code"]
        if not (code.startswith("sh") or code.startswith("sz")):
            raise ValueError(f"Invalid stock code: {code}. Must start with 'sh' or 'sz'.")
        holdings.append(HoldingConfig(
            code=code,
            name=item["name"],
            cost=item.get("cost"),
            shares=item.get("shares"),
            stop_loss=item.get("stop_loss"),
            take_profit=item.get("take_profit"),
        ))

    indices = []
    for item in data.get("indices", []):
        indices.append(IndexConfig(code=item["code"], name=item["name"]))

    return AppConfig(
        holdings=holdings,
        indices=indices,
        stop_loss_pct=data.get("stop_loss_pct", -8.0),
        take_profit_pct=data.get("take_profit_pct", 15.0),
        alert_threshold_pct=data.get("alert_threshold_pct", 2.0),
        sudden_threshold_pct=data.get("sudden_threshold_pct", 1.0),
        poll_interval_seconds=data.get("poll_interval_seconds", 5),
        tencent_api_template=data.get("tencent_api_template", "http://qt.gtimg.cn/q={codes}"),
        db_path=data.get("db_path", "price_history.db"),
        data_sources=DataSourceConfig(
            enabled=data.get("data_sources", {}).get("enabled", ["tencent", "tdx"]),
            successive_fail_limit=data.get("data_sources", {}).get("successive_fail_limit", 3),
            cooldown_seconds=data.get("data_sources", {}).get("cooldown_seconds", 60),
        ),
        news=NewsConfig(
            enabled=data.get("news", {}).get("enabled", False),
            cls=ClsConfig(
                sign=data.get("news", {}).get("cls", {}).get("sign"),
                cookie=data.get("news", {}).get("cls", {}).get("cookie"),
                poll_interval_seconds=data.get("news", {}).get("cls", {}).get("poll_interval_seconds", 30),
                off_hours_poll_interval_seconds=data.get("news", {}).get("cls", {}).get("off_hours_poll_interval_seconds", 300),
            ),
            filter=NewsFilterConfig(
                keyword_threshold=data.get("news", {}).get("filter", {}).get("keyword_threshold", 0.3),
                min_confidence_for_notify=data.get("news", {}).get("filter", {}).get("min_confidence_for_notify", 0.7),
                min_confidence_for_holdings_alert=data.get("news", {}).get("filter", {}).get("min_confidence_for_holdings_alert", 0.5),
            ),
            llm=LlmConfig(
                model=data.get("news", {}).get("llm", {}).get("model", "minimax-m2.5:cloud"),
                host=data.get("news", {}).get("llm", {}).get("host", "http://localhost:11434"),
                api_key=data.get("news", {}).get("llm", {}).get("api_key"),
                max_per_minute=data.get("news", {}).get("llm", {}).get("max_per_minute", 10),
                cache_ttl_hours=data.get("news", {}).get("llm", {}).get("cache_ttl_hours", 24),
                request_timeout_seconds=data.get("news", {}).get("llm", {}).get("request_timeout_seconds", 30),
            ),
            sector=SectorConfig(
                cache_ttl_days=data.get("news", {}).get("sector", {}).get("cache_ttl_days", 7),
                force_refresh=data.get("news", {}).get("sector", {}).get("force_refresh", False),
            ),
        ),
    )
