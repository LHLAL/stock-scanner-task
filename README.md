# A股持仓监控

macOS 状态栏实时看盘工具。在菜单栏显示 A 股持仓股的实时价格，红色上涨、绿色下跌；异动 / 止损 / 止盈 / 趋势反转 / 放量加速时弹出系统通知；支持多路数据源热备。

## 功能特性

- **状态栏实时显示**：标题栏展示 `N↑ M↓` 上涨/下跌家数；菜单内联显示现价、涨跌、盈亏
- **中国股市配色**：上涨 = 红色，下跌 = 绿色（通过 `NSAttributedString` 实现）
- **多数据源热备**：腾讯行情（HTTP） + 通达信（TCP 7709，可选），单源失败自动熔断 + 切换 + 冷却
- **轮询可调**：交易时段 5 秒 / 收盘后 60 秒；`poll_interval_seconds` 自由配置
- **持仓 CRUD**：菜单内置「添加 / 编辑 / 删除」对话框，编辑后自动写回 `config.json`
- **📰 新闻情报**：财联社电报 → 关键词预筛 → 本地 Ollama LLM 分析（含卡脖子瓶颈三指标/四柱/产业趋势） → 板块/个股关联 → 命中持仓或瓶颈信号弹通知（详见下方）
- **多种告警**（全部 30 分钟冷却，避免骚扰）：
  - **阈值告警**：`change_pct` 超过 `alert_threshold_pct` 时弹窗
  - **异动检测**：与近 3 轮均值偏差 > `sudden_threshold_pct` 时弹窗
  - **止损 / 止盈**：基于 `(现价 - 成本) / 成本` 触发，支持单股独立阈值
  - **连续涨跌**：连续 N 轮同向累计（默认 4 轮）
  - **趋势转多 / 转空**：5 日 SMA 与 20 日 SMA 交叉（基于 SQLite 历史）
  - **放量下跌**：成交量 > 上轮 2 倍且下跌
  - **加速下跌**：下跌速度环比加大 ≥ 1.5%
- **SQLite 历史存储**：每轮价格写入 `price_history.db`，用于趋势判断（`get_recent` / `calc_sma`）
- **收盘状态提示**：非交易时段标题栏加 🕐 标记
- **后台运行**：支持 `start / stop / restart / status` 进程管理
- **菜单打开时持续刷新**：`performSelectorOnMainThread` 在主线程更新 UI，避免阻塞

## 技术栈

- Python 3 + rumps（macOS 状态栏框架）
- requests + pyobjc
- mootdx（可选，TDX 数据源依赖）
- akshare（板块词典来源）
- Ollama（本地 LLM 分析；默认 `minimax-m2.5:cloud`）
- SQLite 3（内置）

## 快速开始

```bash
# 终端启动（后台运行）
cd /Users/apple/Downloads/vscode_space/stock-scanner-task
./run.sh start

# 查看状态
./run.sh status

# 停止监控
./run.sh stop
```

或者在 Finder 中双击 `start.command` 前台启动（按 Ctrl+C 或关闭终端停止）。

首次运行会自动创建 Python 虚拟环境并安装依赖。

## 命令参考

| 命令 | 说明 |
|------|------|
| `./run.sh` | 启动（默认 start） |
| `./run.sh start` | 后台启动 |
| `./run.sh stop` | 停止监控 |
| `./run.sh restart` | 重启 |
| `./run.sh status` | 查看运行状态 |

## 配置文件

编辑 `config.json` 自定义持仓股和参数：

```json
{
  "holdings": [
    {"code": "sz002739", "name": "儒意电影", "cost": 10.50, "shares": 1000, "stop_loss": -5.0, "take_profit": 10.0},
    {"code": "sh600028", "name": "中国石化", "cost": 5.80, "shares": 2000},
    {"code": "sh600339", "name": "中油工程"}
  ],
  "indices": [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"}
  ],
  "stop_loss_pct": -8.0,
  "take_profit_pct": 15.0,
  "alert_threshold_pct": 2.0,
  "sudden_threshold_pct": 1.0,
  "poll_interval_seconds": 5,
  "db_path": "price_history.db",
  "data_sources": {
    "enabled": ["tencent", "tdx"],
    "successive_fail_limit": 3,
    "cooldown_seconds": 60
  }
}
```

持仓项不带 `cost` 和 `shares` 的为纯看盘（不计算盈亏）。

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `holdings` | 持仓列表，支持独立止损/止盈 | — |
| `indices` | 大盘指数列表 | — |
| `stop_loss_pct` | 全局止损线（百分比） | -8.0 |
| `take_profit_pct` | 全局止盈线（百分比） | 15.0 |
| `alert_threshold_pct` | 涨跌幅告警阈值（百分比） | 2.0 |
| `sudden_threshold_pct` | 异动检测阈值（与近 3 轮均值的偏差） | 1.0 |
| `poll_interval_seconds` | 轮询间隔（秒） | 5 |
| `db_path` | SQLite 历史库路径 | `price_history.db` |
| `data_sources.enabled` | 启用的数据源列表（按顺序轮换） | `["tencent", "tdx"]` |
| `data_sources.successive_fail_limit` | 连续失败多少次后熔断 | 3 |
| `data_sources.cooldown_seconds` | 熔断后冷却秒数 | 60 |

### 持仓字段说明

| 字段 | 说明 | 必填 |
|------|------|------|
| `code` | 股票代码（sh/sz 前缀） | 是 |
| `name` | 股票名称 | 是 |
| `cost` | 持仓成本价 | 否（不填则只看盘） |
| `shares` | 持仓股数 | 否 |
| `stop_loss` | 独立止损线（覆盖全局） | 否 |
| `take_profit` | 独立止盈线（覆盖全局） | 否 |

### 数据源说明

| 名称 | 协议 | 依赖 | 限流风险 | 用途 |
|------|------|------|---------|------|
| `tencent` | HTTP `qt.gtimg.cn` | 无 | 低 | 默认主源 |
| `tdx` | TCP 7709（mootdx） | `pip install mootdx` | 无（内网） | 备援 |

> `enabled` 列表中的数据源按顺序注册，成功抓取后该源排到队尾，避免单源高频触发风控。某源连续失败 `successive_fail_limit` 次后进入 `cooldown_seconds` 秒冷却。所有源都失败时返回最近一次缓存数据。

### 股票代码格式

| 前缀 | 交易所 | 代码范围 |
|------|--------|----------|
| `sh` | 上海证券交易所 | 600xxx, 601xxx, 603xxx |
| `sz` | 深圳证券交易所 | 000xxx, 001xxx, 002xxx, 300xxx |

> 注意：`002` 开头的中小板和 `300` 开头的创业板属于深圳交易所，必须使用 `sz` 前缀。

## 项目结构

```
stock-scanner-task/
├── config.json          # 持仓股 + 全局参数
├── price_history.db     # SQLite 历史（自动生成）
├── stock-monitor.log    # 运行日志（自动生成，10MB × 5 备份）
├── README.md            # 本文件
├── docs/
│   ├── multi-source-fetcher-design.md   # 多源架构设计文档
│   ├── news-intelligence-design.md      # 新闻情报模块设计文档
│   └── sector_dict.json                 # 板块→代表股 映射
├── run.sh               # 终端启动/停止脚本
├── start.command        # Finder 双击启动脚本
├── stock_monitor.py     # 主入口（日志初始化 + 启动 App）
└── app/
    ├── __init__.py
    ├── config.py        # 配置 dataclass + JSON 加载/保存
    ├── multi_fetcher.py # StockQuote + TencentSource / TDXSource + CircuitBreaker + RotatingMultiFetcher
    ├── storage.py       # PriceDB（SQLite 历史 + news_cache + news_analysis）
    ├── monitor.py       # PriceMonitor（异动 / 趋势 / 加速 / 放量 / 连续 / 止损止盈）
    ├── menu_bar.py      # rumps UI + AppKit 富文本 + NSObject 主线程调度
    └── news/            # 新闻情报模块
        ├── models.py    # RawNews + NewsAnalysis dataclass
        ├── fetcher.py   # ClsFetcher（财联社官方 JSON API）
        ├── analyzer.py  # OllamaAnalyzer + KeywordPreFilter + TokenBucket
        ├── sector.py    # SectorMapper（板块词典模糊匹配）
        └── monitor.py   # NewsMonitor 主循环（增量轮询 + 节流 + 通知）
```

## 告警类型速查

| 类型 | 触发条件 | 弹窗内容 |
|------|---------|---------|
| 阈值 | `|change_pct| ≥ alert_threshold_pct` | `名称 涨/跌 +/-X.XX%` |
| 异动 | `|change_pct - 近3轮均值| ≥ sudden_threshold_pct` | `名称 异动涨/异动跌` |
| 止损 | `(现价-成本)/成本 ≤ stop_loss`（或个股阈值） | `🚨 止损: 名称` |
| 止盈 | `(现价-成本)/成本 ≥ take_profit`（或个股阈值） | `🎯 止盈: 名称` |
| 连续上涨 / 连续下跌 | 连续 N 轮同向（N = 4） | `📊 名称 连续上涨/下跌 N 轮` |
| 趋势转多 / 转空 | 价格上穿 / 下穿 5 日 SMA | `名称 上穿均线，趋势转多` |
| 放量下跌 | 成交量 > 上轮 × 2 且价格下跌 | `名称 放量 X.X 倍 下跌 X.X%` |
| 加速下跌 | 下跌速度环比加大 ≥ 1.5% | `名称 加速下跌 X.X%` |

所有告警统一 30 分钟冷却（`alert_cooldown`），同一只股票同一类型不会重复骚扰。

## 日志

运行日志输出到 `stock-monitor.log`（10 MB × 5 备份自动轮转）：

```bash
tail -f stock-monitor.log
```

启动时会打印持仓数 / 指数数 / 刷新间隔；之后每 60 轮（约 5 分钟）打一条心跳。

## 新闻情报模块

财联社电报 → 关键词预筛 → 本地 Ollama LLM → 板块关联 → 命中持仓弹通知。开启后菜单栏新增「📰 新闻分析」子菜单，列最近 10 条分析结果。

### 前置

```bash
# 1. 安装 Ollama 并启动 daemon
brew install ollama
ollama serve &  # 或 launchctl / brew services

# 2. 配置 API key（用于 cloud 模型；留空走本地模型）
export OLLAMA_API_KEY=your_key_here
echo 'export OLLAMA_API_KEY=your_key_here' >> ~/.zshrc

# 3. 编辑 config.json，将 news.enabled 改为 true
```

### 启用

`config.json` 加 `"news"` 段（默认模板已带，置 `enabled: true` 即可）：

```json
{
  "news": {
    "enabled": true,
    "cls": {"sign": null, "cookie": null, "poll_interval_seconds": 20, "off_hours_poll_interval_seconds": 600},
    "filter": {"keyword_threshold": 0.5, "min_confidence_for_notify": 0.8, "min_confidence_for_holdings_alert": 0.65, "bottleneck_confidence_floor": 0.65, "max_notifications_per_day": 20},
    "llm": {"model": "minimax-m2.5:cloud", "host": "http://localhost:11434", "api_key": null,
            "max_per_minute": 12, "cache_ttl_hours": 24, "request_timeout_seconds": 30, "daily_limit": 1000},
    "sector": {"cache_ttl_days": 7, "force_refresh": false}
  }
}
```

### 行为

- **盘中 30 秒 / 盘后 5 分钟** 自动切换轮询频率（复用 `is_market_open()`）
- **关键词预筛**（阈值 0.5）：约 160 词覆盖**卡脖子投资哲学**（订单/产能/满产/CPO/HBM/磷化铟/人形机器人/光刻机 等主题词）+ 政策/财报/订单信号；同时 26 词**黑名单**（美股/港股/美光/英伟达/美玉米 等海外噪声）降权
- **LLM 分析**（按"卡脖子供应链瓶颈理论"）：本地 Ollama 调用 `minimax-m2.5:cloud`，强制 JSON 输出，包含 5 维核心 + 瓶颈 9 维增强
- **板块匹配**：LLM 输出 sectors 通过 fuzzy match 映射到 `docs/sector_dict.json`（约 70 板块 / 230 只代表股）
- **通知触发（三层门控 + 噪声过滤）**：
  - **噪声过滤**: `confidence < 0.5` 一律不通知（即使 LLM 返回 bullish 也降级）
  - **Tier 1**: `confidence ≥ 0.8` 且非中性
  - **Tier 2**: 命中持仓 + `confidence ≥ 0.65` 且非中性
  - **Tier 3**: 卡脖子信号 + `confidence ≥ 0.65` 且非中性
  - **日上限**: 默认 20 次/天，超限只入库不弹通知
  - **方向强制**: `direction == neutral` 一律不通知
- **缓存**：news_cache 表去重 + 24h LLM 结果缓存

### LLM 分析维度

#### 核心 5 维（基础）

| 维度 | 字段 | 用途 |
|------|------|------|
| 影响对象 | `sectors` + `stocks` | 板块匹配 + 命中持仓 |
| 影响方向 | `direction` (bullish/bearish/neutral) | emoji + 标签 |
| 置信度 | `confidence` (0-1) | 通知门控 |
| 影响时效 | `time_horizon` (intraday/next_day/weekly) | 预留过滤 |
| 因果推理 | `rationale` (≤80字) | 通知正文 |

#### 瓶颈 9 维（增强）— 卡脖子投资哲学

**三硬指标**（订单/产能/毛利率）：

| 字段 | 取值 |
|------|------|
| `bottleneck_order_signal` | `none` / `mentioned` / **`strong`**（龙头排产排满 + 溢价拿货） |
| `bottleneck_capacity_signal` | `none` / `expansion` / **`utilization_high`**（满产） / `inventory_warning`（扩产过快库存积压） |
| `bottleneck_margin_signal` | `unknown` / **`rising`**（稳中有升 = 卡脖子核心证据）/ `stable` / `declining`（壁垒被破） |

**卡脖子四柱**：

| 字段 | 取值 |
|------|------|
| `is_kneck` | true / false |
| `scarcity_pillars` | 数组：`tech_moat`（技术代差）/`single_point`（单点刚需）/`certification`（3-5年认证）/`long_cycle`（长周期） |

**产业趋势判断**：

| 字段 | 取值 |
|------|------|
| `news_category` | policy/order/capacity/financial/patent/supply_disruption/general |
| `narrative_themes` | 标签数组：`AI算力`/`CPO`/`人形机器人`/`半导体设备`/`国产替代`/`光通信`/`特种气体`/`磷化铟`/... |
| `trend_horizon_years` | 1-10（不可逆变化的窗口期） |
| `industry_certainty` | `speculative`（投机）→ `emerging`（萌芽）→ `established`（确立）→ `dominant`（主导） |

#### 通知与菜单增强

- **badge 聚合**：`is_kneck` + 订单爆发 + 满产 + 毛利率上升 → `🔧卡脖子 📈订单爆发 ⚡满产 💹毛利率↑`
- **菜单子项**显示：类型 + 卡脖子四柱 + 主题 + 三硬指标 + 趋势确定性 + 理由
- **通知正文**：显示卡脖子稀缺性、主题、确定性、命中持仓

#### 实际效果样例

输入：「中际旭创：800G光模块订单排至2027年，产能持续满产」

```
summary: 800G光模块订单排至2027年，产能满产毛利率高，AI算力驱动CPO技术路线加速
direction: bullish (0.85)
news_category: order
is_kneck: True
scarcity_pillars: [tech_moat, single_point, long_cycle]
bottleneck_order_signal: strong
bottleneck_capacity_signal: utilization_high
bottleneck_margin_signal: stable
narrative_themes: [AI算力, CPO, 光通信, 国产替代]
industry_certainty: emerging / 8y
badge: 🔧卡脖子 📈订单爆发 ⚡满产
```

### 限流

- LLM 调用令牌桶：默认 12/min（可调 `llm.max_per_minute`）
- LLM **日上限**：默认 1000/天（`llm.daily_limit`）。超限自动跳过 LLM 调用直到次日 0 点；90% 触发警告日志；设 `0` 禁用
- 新闻本身无速率限制（CLS 官方 API 免费）
- 健康检查失败时自动降级为「只抓新闻不分析」

### 自定义板块词典

`docs/sector_dict.json` 是手工维护的板块→代表股映射。要修改：

1. 编辑 JSON（板块名 + 股票代码数组）
2. 重启程序生效

LLM 输出的板块名会按 (1) 精确 (2) 子串 (3) SequenceMatcher 模糊匹配 顺序尝试匹配本地键。

## 常见问题

**Q: 股票查不到？**  
检查股票代码前缀是否正确。`002` 和 `300` 开头是深圳股票，必须用 `sz` 前缀。

**Q: 菜单打开时数据不刷新？**  
已通过 `performSelectorOnMainThread_withObject_waitUntilDone_` 在主线程异步更新 UI，菜单展开时不影响数据刷新。

**Q: 新闻模块不工作？**  
启动时日志会打印 `📰 新闻模块: cls=... llm=... sector=...` 健康状态。
- `cls=False`：财联社接口不可访问，检查网络
- `llm=False`：Ollama daemon 未跑或 API key 错误。`curl http://localhost:11434/api/tags` 应返回 JSON
- `sector=False`：`docs/sector_dict.json` 缺失或损坏

**Q: 新闻分析里"半导体"但持仓没有半导体股，要不要弹通知？**  
不弹。`hits_holdings` 才会触发通知；`is_high_confidence` 才会显示在子菜单标题"📰 新闻分析 (N)" 里。两个都满足才弹系统通知。

**Q: 所有数据源都失败怎么办？**  
返回最近一次成功抓取的缓存数据，并在日志中输出 `ERROR: All sources failed`。检查本机是否能访问 `qt.gtimg.cn`（可能需要关闭代理）。

**Q: 怎么禁用通达信数据源？**  
把 `data_sources.enabled` 改为 `["tencent"]`，或直接卸载 `mootdx` 让 `HAS_MOOTDX` 变 `False`。

**Q: 不想收到告警弹窗？**  
系统设置 → 通知 → 搜索「监控」或「Python」调整即可（macOS 用 Python launcher 时通知源标识可能不同）。
