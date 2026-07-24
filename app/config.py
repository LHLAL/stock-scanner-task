import json
import os
from dataclasses import dataclass, field
from typing import List, Optional


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
    )
