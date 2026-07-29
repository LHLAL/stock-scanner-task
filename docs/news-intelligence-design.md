# 新闻情报模块设计方案

> 目标：在 `stock-scanner-task` 现有持仓监控基础上，新增 **财联社电报 → 高置信度过滤 → 本地 LLM 分析 → 关联板块/个股涨跌预测** 的情报流。
>
> 状态：**方案已根据实测调整**，待评审，未实施。

---

## 一、目标与非目标

### 1.1 目标

1. 实时获取财联社电报新闻（直接调官方 JSON API，免爬虫）
2. 通过关键词预筛 + LLM 自评双层过滤，识别 **高置信度** 新闻
3. 对高置信度新闻调用 **本地 Ollama** 分析：
   - 涉及的 **行业板块**（半导体 / 锂电 / 白酒 / 银行 …）
   - 关联的 **个股**
   - **预期方向**（看多 / 看空 / 中性）+ **置信度**
4. 在菜单栏新增「📰 新闻」子菜单；命中持仓或置信度极高时弹系统通知
5. 与现有 holdings / 多源行情 / SQLite 历史解耦，独立模块

### 1.2 非目标

- ❌ 不做自动交易（只生成信号，由人决策）
- ❌ 不做历史回测框架（v1 不含）
- ❌ 不做情感分析之外的 NLP（实体识别 / 知识图谱）
- ❌ 不接付费 CLS API（直接调免费 JSON 接口）

---

## 二、实测确认（关键依赖验证）

| 依赖 | 实测结果 |
|------|---------|
| **CLS JSON API** | ✅ 工作。用户提供的 `https://www.cls.cn/api/cache?app=CailianpressWeb&...&name=telegraphList` 返回结构化 JSON，无需爬虫 |
| **本地 Ollama** | ⚠️ 仅装 `lfm-embed`（embedding 模型）+ 3 个 `:cloud` 模型（含 `minimax-m2.5:cloud`，需 API key） |
| **mootdx block/pool** | ❌ 不行。`block()` 返回 386k 行，90% 是 `\x00600113\x006` 这种 mojibake；`pool()` 是空实现 |
| **akshare 板块** | ✅ 装得上（1.18.80），但默认走代理 `127.0.0.1:7897` 不稳定。绕过代理后能从东方财富拉到 496 个行业板块 |
| **腾讯行情 + TDX** | 已有，CLS 数据流独立 |

---

## 三、架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                      StockMenuBarApp                              │
│   ┌──────────────────┐   ┌──────────────────┐   ┌────────────┐   │
│   │ PriceMonitor     │   │ NewsMonitor      │   │ PriceDB    │   │
│   │ (已存在)         │   │ (新增)           │   │ (已存在)   │   │
│   └──────────────────┘   └────────┬─────────┘   └────────────┘   │
│                                   │                               │
└───────────────────────────────────┼───────────────────────────────┘
                                    │
        ┌───────────────────────────┼──────────────────────────────┐
        ▼                           ▼                              ▼
┌───────────────┐          ┌─────────────────┐            ┌────────────────┐
│ ClsFetcher    │          │ LlmAnalyzer     │            │ SectorMapper   │
│ (官方 JSON)   │ ───────▶ │ (本地 Ollama)   │ ────────▶  │ (东方财富缓存) │
└───────┬───────┘          └────────┬────────┘            └────────┬───────┘
        │                           │                              │
        │                           ▼                              ▼
        │                  ┌─────────────────┐            ┌────────────────┐
        │                  │ Ollama HTTP     │            │ docs/sector_   │
        │                  │ localhost:11434 │            │ dict.json      │
        │                  └─────────────────┘            └────────────────┘
        ▼
┌──────────────────────────────────────────────────┐
│  https://www.cls.cn/api/cache?...&name=          │
│       telegraphList                              │
└──────────────────────────────────────────────────┘
```

### 数据流

```
[T+30s]  ClsFetcher.fetch(last_time)        ← 增量轮询
            │ (List[RawNews])
            ▼
         Deduper (SHA256 hash + SQLite TTL=24h)
            │
            ▼
         KeywordPreFilter (权重词典，过滤 80% 噪声)
            │ 通过 (≥ 0.3)
            ▼
         LlmAnalyzer.analyze()               ← Ollama /api/generate，max N/min
            │ (NewsAnalysis: sectors/stocks/direction/confidence)
            ▼
         SectorMapper.match()                ← docs/sector_dict.json（东方财富缓存）
            │
            ▼
         NewsMonitor 聚合：
           - 入库 SQLite news_analysis 表
           - 与 holdings 求交集
           - 命中或 confidence ≥ 0.85 → 系统通知
           - 更新「📰 新闻」UI 子菜单
```

---

## 四、模块拆分

新增 5 个文件 + 1 个数据文件：

```
app/
├── news/                       # 新建子包
│   ├── __init__.py
│   ├── fetcher.py              # ClsFetcher（官方 JSON API + Cookie/signature 维护）
│   ├── analyzer.py             # LlmAnalyzer（Ollama HTTP client）
│   ├── sector.py               # SectorMapper（缓存 + 远程拉取 + 模糊匹配）
│   └── models.py               # RawNews, NewsAnalysis dataclass
├── config.py                   # + NewsConfig / ClsConfig / LlmConfig / SectorConfig dataclass
├── monitor.py                  # 现有 PriceMonitor 不动
├── menu_bar.py                 # + 「📰 新闻」子菜单 + 通知触发
└── storage.py                  # + news_cache + news_analysis 表

docs/
└── sector_dict.json            # 板块→代表股 映射（约 100 个板块，自动生成）

config.json                     # + "news" 段
```

不改动 `multi_fetcher.py` / `monitor.py` 核心逻辑；`storage.py` 仅加新表；`menu_bar.py` 加新子菜单。

---

## 五、关键模块设计

### 5.1 CLS 接入（官方 JSON API）

**接口**（实测可用）：

```
GET https://www.cls.cn/api/cache?
    app=CailianpressWeb
    &lastTime=<上次最新条目 ctime>
    &name=telegraphList
    &os=web
    &sv=8.7.9
    [&sign=<可选，不传也行>]
```

> **实测**：不传 `sign` 或传 `sign=` 空字符串均能正常返回数据（`errno: 0`）。CLS 服务端对 `telegraphList` 这个 endpoint 不强制校验签名。`sign` 字段**保留为可选**，方便未来其他 endpoint 需要时直接启用。

**Headers 关键字段**：
- `referer: https://www.cls.cn/telegraph`（必须）
- `user-agent: Chrome/150`（必须）
- `cookie: ...`（可选，无 cookie 也能返回数据；带 cookie 可拿 VIP/付费内容）

**响应结构**：

```json
{
  "errno": 0,
  "data": {
    "roll_data": [],                  // 普通电报
    "vip": [                          // VIP/电报解读（高质量）
      {
        "id": 2439153,
        "type": 20026,
        "type_name": "电报解读",
        "title": "【机构龙虎榜解读】脑机接口+AI医疗+机器人...",
        "ctime": 1785234596,           // ← 用于下次 lastTime
        "content": "...",
        "subject": "..."               // 主题分类
      }
    ]
  }
}
```

**增量策略**：

```python
class ClsFetcher:
    def __init__(self, cookie: Optional[str] = None):
        self._last_time: Optional[int] = None
        self._cookie = cookie or self._default_cookie()
    
    def fetch(self) -> List[RawNews]:
        params = {
            "app": "CailianpressWeb",
            "name": "telegraphList",
            "os": "web",
            "sv": "8.7.9",
        }
        if self._sign:                              # 可选：未来 CLS 加固时启用
            params["sign"] = self._sign
        if self._last_time:
            params["lastTime"] = self._last_time
        
        resp = requests.get(URL, params=params, headers=self._headers, timeout=10)
        data = resp.json()["data"]
        items = data.get("roll_data", []) + data.get("vip", [])
        
        if items:
            self._last_time = max(i["ctime"] for i in items)
        return [self._to_raw_news(i) for i in items]
```

**签名问题**：实测发现 `telegraphList` endpoint 不强制校验 sign。**v1 不需要 sign 处理**——直接调用即可。

> 如果未来 CLS 加固或换 endpoint（需要 sign），三种 fallback 方案对比：

| 方案 | 部署成本 | 维护成本 | 稳定性 | 适用场景 |
|------|---------|---------|--------|---------|
| **A. 硬编码 sign + 手动更新** | 0 | 每几天手动抓一次（5 min） | 中（依赖人记得） | 用户每天用，偶尔失效可接受 |
| **B. Playwright 自动抓** | +200MB Chromium + 启动 ~1s/次 | 0 | 高（每次都拿最新） | 完全免维护场景 |
| **C. JS 逆向算 sign** | 中（需分析 JS 算法） | 中（CLS 更新 JS 会失效） | 中 | 性能敏感 + 不想装 Chromium |

**v1 不需要**；保留 `sign` 字段在配置中（默认 `null`），仅当未来真正需要时再启用。

### 5.2 Ollama Cloud 模型接入

**HTTP API**（Ollama 内置）：

```
POST http://localhost:11434/api/generate
Headers: Authorization: Bearer <OLLAMA_API_KEY>
{
  "model": "minimax-m2.5:cloud",
  "prompt": "...",
  "stream": false,
  "format": "json",
  "options": {"temperature": 0.1}
}
```

**模型**（v1 默认）：

| 模型 | 类型 | 中文 | 速度 | 成本 |
|------|------|------|------|------|
| `minimax-m2.5:cloud` | Cloud（Ollama 托管） | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | 按 Ollama 计费 |
| `qwen2.5:7b` | 本地（备选） | ⭐⭐⭐⭐⭐ | 中 | 0 |

**默认选 `minimax-m2.5:cloud`**：用户 platform 是 MiniMax，cloud 模型中文能力强，免去本地 4.4GB 下载。

**API Key 配置**（三种优先级）：

```python
def _resolve_api_key(config_value: Optional[str]) -> Optional[str]:
    return (
        config_value                                 # 1. config.json news.llm.api_key
        or os.environ.get("OLLAMA_API_KEY")           # 2. 环境变量
        or os.environ.get("OLLAMA_KEY")               # 3. 别名
    )
```

- **推荐用环境变量**：`export OLLAMA_API_KEY=...`（避免明文进 git）
- **config.json 兜底**：方便临时切换；`.gitignore` 默认忽略密钥明文
- **启动检测**：缺失 → 警告 + 新闻分析禁用，价格监控照常运行

**Prompt**（要求 JSON 结构化输出）：

```python
NEWS_ANALYSIS_PROMPT = """你是 A 股市场分析师。请阅读以下新闻，给出严格 JSON 分析：

【新闻标题】 {title}
【新闻内容】 {content}

输出（必须是合法 JSON，不要 markdown 包装）:
{{
  "summary": "<一句话总结，≤30字>",
  "sectors": ["<行业板块1>", "<行业板块2>"],
  "stocks": ["<股票名或代码>"],
  "direction": "<bullish | bearish | neutral>",
  "confidence": <0.0-1.0>,
  "time_horizon": "<intraday | next_day | weekly>",
  "rationale": "<≤80字推理>"
}}

规则：
- 只在明确政策/数据/事件时给 bullish/bearish，模糊时给 neutral + confidence < 0.5
- 不要给投资建议，只做事实分析
- sectors 必须是行业大类（半导体/白酒/银行等），不要给个股
"""
```

**`LlmAnalyzer`**：

```python
class OllamaAnalyzer:
    def __init__(self, model: str = "minimax-m2.5:cloud",
                 host: str = "http://localhost:11434",
                 api_key: Optional[str] = None):
        self._model = model
        self._host = host
        self._api_key = api_key or os.environ.get("OLLAMA_API_KEY")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def analyze(self, raw_news: RawNews) -> NewsAnalysis:
        prompt = NEWS_ANALYSIS_PROMPT.format(
            title=raw_news.title, content=raw_news.content[:1000])
        resp = requests.post(
            f"{self._host}/api/generate",
            headers=self._headers(),
            json={"model": self._model, "prompt": prompt, "stream": False,
                  "format": "json", "options": {"temperature": 0.1}},
            timeout=30,
        )
        resp.raise_for_status()
        return NewsAnalysis.from_json(resp.json()["response"])

    def health_check(self) -> bool:
        """同时验证 daemon 在跑 + 模型已下载/可用。"""
        try:
            r = requests.get(f"{self._host}/api/tags",
                             headers=self._headers(), timeout=3)
            r.raise_for_status()
            models = [m["name"] for m in r.json()["models"]]
            return any(self._model in m for m in models)
        except Exception:
            return False
```

**401/403 错误处理**：捕获 `HTTPError` → 标记 `auth_failed`，下次请求跳过直到用户重启配置。

### 5.3 高置信度过滤

**第一层：关键词预筛**（本地、零成本）

```python
HIGH_IMPACT_KEYWORDS = {
    # 政策 / 监管 (权重 1.0)
    "央行": 1.0, "证监会": 1.0, "国务院": 1.0, "政治局": 1.0,
    "降准": 1.0, "降息": 1.0, "加息": 1.0, "上调存款准备金率": 1.0,
    # 经济数据 (权重 0.9)
    "GDP": 0.9, "CPI": 0.9, "PPI": 0.9, "PMI": 0.9, "M2": 0.8,
    # 重大事件 (权重 0.8-1.0)
    "突发": 1.0, "重大": 0.9, "确认": 0.8, "数据": 0.7,
    "业绩": 0.7, "财报": 0.7, "中标": 0.7, "收购": 0.7, "重组": 0.7,
    # 反向（噪声）(权重 -0.5 ~ -1.0)
    "邀请": -0.5, "福利": -0.5, "抽奖": -0.8, "直播": -0.3,
    "广告": -0.7, "活动": -0.3,
}

def keyword_score(title: str, content: str) -> float:
    text = title + " " + content[:200]
    return max((w for k, w in HIGH_IMPACT_KEYWORDS.items() if k in text), default=0.0)
```

**通过条件**：分数 ≥ `keyword_threshold` (默认 0.3) 才送 LLM。

**第二层：LLM 自评**

```python
def is_high_confidence(analysis: NewsAnalysis, threshold: float = 0.7) -> bool:
    return analysis.confidence >= threshold and analysis.direction != "neutral"
```

### 5.4 板块映射

**数据源**：东方财富行业板块（496 个），通过 HTTP 直接拉，绕过代理：

```python
EAST_MONEY_SECTOR_URL = (
    "https://17.push2.eastmoney.com/api/qt/clist/get"
    "?pn=1&pz=500&po=1&np=1&fs=m:90+t:2+f:!50"
    "&fields=f1,f2,f3,f12,f14"
)
```

**拉取 + 缓存策略**：

```python
class SectorMapper:
    CACHE_FILE = Path(__file__).parent.parent.parent / "docs" / "sector_dict.json"
    CACHE_TTL_DAYS = 7
    
    def __init__(self, refresh: bool = False):
        if refresh or not self._is_fresh():
            self._dict = self._fetch_from_eastmoney()      # 远程
            self._save_cache()                              # 本地 JSON
        else:
            self._dict = json.loads(self.CACHE_FILE.read_text())
    
    def _fetch_from_eastmoney(self) -> Dict[str, List[str]]:
        # 1. 拉所有行业板块名
        # 2. 对每个板块调 stock_board_industry_cons_em 拿成分股
        # 3. 转成 {"半导体": ["sh688981", ...], "白酒": ["sh600519", ...]}
        ...
    
    def match(self, sector_name: str) -> List[str]:
        # 模糊匹配（difflib）
        for key in self._dict:
            if sector_name in key or key in sector_name:
                return self._dict[key]
        return []
```

**绕过代理**：

```python
# 在 fetch 时禁用代理（绕过 127.0.0.1:7897）
proxies = {"http": None, "https": None}
resp = requests.get(url, proxies=proxies, timeout=10)
```

### 5.5 UI 集成

菜单栏新增结构：

```
[🕐 | N↑ M↓]
├─ 持仓: ¥1234 (+1.5%) | 今日: ¥56
├─ 上证指数: ...
├─ 儒意电影: ...
├─ 📰 新闻分析 (N 条)        ← 新增
│  ├─ 🟢 [半导体] 国家大基金注资中芯国际...
│  │   ↗ 利好 sh688981, sz002371  ← 关联持仓加 🔔
│  │   置信度 0.85 · 12:34
│  ├─ 🔴 [白酒] 消费税上调预期...
│  │   ↘ 利空 sh600519  ← 命中持仓
│  │   置信度 0.78 · 12:28
│  └─ ⚪ [新能源汽车] 工信部表态...
│       → 中性 置信度 0.55 · 12:20
├─ 刷新于: 12:34:56
└─ 退出
```

**通知触发条件**：

```python
should_notify = (
    analysis.confidence >= cfg.filter.min_confidence_for_notify
    or (
        analysis.confidence >= cfg.filter.min_confidence_for_holdings_alert
        and any(s in {h.code for h in holdings} for s in mapped_stocks)
    )
)
```

### 5.6 配置 schema

`config.json` 新增 `news` 段：

```json
{
  "news": {
    "enabled": true,
    "cls": {
      "sign": null,
      "cookie": null,
      "poll_interval_seconds": 30,
      "off_hours_poll_interval_seconds": 300
    },
    "filter": {
      "keyword_threshold": 0.3,
      "min_confidence_for_notify": 0.7,
      "min_confidence_for_holdings_alert": 0.5
    },
    "llm": {
      "model": "minimax-m2.5:cloud",
      "host": "http://localhost:11434",
      "api_key": null,
      "max_per_minute": 10,
      "cache_ttl_hours": 24,
      "request_timeout_seconds": 30
    },
    "sector": {
      "cache_ttl_days": 7,
      "force_refresh": false
    }
  }
}
```

---

## 六、节流 / 缓存 / 成本

| 维度 | 策略 |
|------|------|
| **LLM 调用频率** | `max_per_minute=10`（令牌桶），超出排队不丢 |
| **新闻去重** | SHA256(content) → SQLite `news_cache` 表，TTL 24h |
| **LLM 结果缓存** | 同 hash 新闻 24h 内复用上次结果 |
| **预筛优先** | 关键词过滤 80% 噪声，根本不调 LLM |
| **新闻抓取频率** | 自动切换：盘中 30 秒 / 盘后 5 分钟（复用 `monitor.is_market_open()`） |
| **代理绕过** | 东方财富/CLS 接口直连，禁用系统代理 |
| **失败兜底** | Ollama 不可用 → 新闻继续抓但不分析，UI 标注"AI 暂不可用" |
| **CLS 接口变化** | `errno != 0` 时记录日志 + 暂停 1 分钟；连续 3 次失败 → 暂停 5 分钟（简单的退避） |

**成本估算**：

| 项 | 单价 | 年用量 | 年成本 |
|----|------|--------|--------|
| Ollama cloud `minimax-m2.5:cloud` | 按 Ollama 计费 | ~16,000 次/年（关键词过滤后） | 待核实 Ollama 定价 |
| CLS 官方 JSON API | 免费 | — | 0 |
| akshare（板块缓存） | 免费 | — | 0 |

**对照**：本地 `qwen2.5:7b` 模型 0 元，但 4.4GB 下载 + 占用内存 + 推理 3-5s/次。Cloud 模型换 0 部署成本，代价是按调用付费 + 需联网 + 需 API key。

---

## 七、风险与缓解

| 风险 | 缓解 |
|------|------|
| CLS 接口变更/下线 | 退避重试；连续失败后暂停抓取 + 通知用户；预留 `sign` 配置字段，未来加固时启用 |
| Ollama 未启动 / API key 无效 | `health_check()` 探测；降级为「只抓新闻不分析」；UI 提示 |
| LLM 幻觉（捏造板块/股票） | 与本地词典交叉验证；词典外的股票标记「待人工确认」 |
| Ollama 推理慢（7B ~3-5s/次） | 限流 + 异步队列；不影响价格刷新线程 |
| 代理 127.0.0.1:7897 不稳定 | 抓 CLS/东方财富时显式 `proxies={"https": None}` |
| 菜单栏子菜单过多导致卡顿 | 限制最近 20 条；超过则折叠为「查看更多」 |
| 配置错误（API key 缺失） | 启动检测；新闻模块禁用不影响主监控 |
| 高频垃圾新闻淹没信号 | 关键词预筛 + LLM 双层过滤；保留原文供回溯 |

---

## 八、实施路径

按依赖顺序分 4 阶段：

### Phase 1 — 骨架（1 天）
- [ ] `app/news/` 子包 + `models.py`
- [ ] `ClsFetcher`（官方 JSON API + 增量 lastTime）
- [ ] SQLite `news_cache` 表
- [ ] `config.json` 加 news 段 + `NewsConfig` dataclass
- [ ] 启动时探测 Ollama + CLS 健康

### Phase 2 — 过滤 + LLM（2 天）
- [ ] `KeywordPreFilter`（权重词典）
- [ ] `OllamaAnalyzer` + Prompt + JSON 解析 + 重试
- [ ] LLM 结果缓存

### Phase 3 — 板块 + UI（1-2 天）
- [ ] `SectorMapper`（东方财富拉取 + 本地 JSON 缓存 + 模糊匹配）
- [ ] `NewsMonitor` 主循环（与 PriceMonitor 并行）
- [ ] `menu_bar.py` 加「📰 新闻」子菜单
- [ ] 通知触发逻辑

### Phase 4 — 调优（持续）
- [ ] 节流参数 + 关键词词典扩充
- [ ] 可选：playwright 自动更新 CLS sign（仅当 CLS 加固时）
- [ ] 失败监控 + 告警

**用户前置准备**：
1. Ollama 账号登录 + 拿到 `OLLAMA_API_KEY`（用于 cloud 模型鉴权；环境变量推荐，避免明文进 git）
   ```bash
   export OLLAMA_API_KEY=your_key_here
   # 或写入 ~/.zshrc / ~/.bashrc 持久化
   ```
2. **无需**浏览器抓 CLS sign/cookie（v1 不需要）
3. **无需** `ollama pull`（cloud 模型自动按需下载元数据）

---

## 九、用户决策记录

| 决策点 | 选择 |
|--------|------|
| LLM 模型 | `minimax-m2.5:cloud`（Ollama Cloud） |
| API key 存储 | 环境变量 `OLLAMA_API_KEY` 优先，config.json 兜底 |
| 「关注股」概念 | **不扩展**，只对现有 holdings 弹通知 |
| 盘中/盘后频率 | **自动切换**（30s vs 5min，用现有 `is_market_open()` 判断） |
| CLS sign | **不需要**（实测 `telegraphList` 不强制校验）；保留字段防未来加固 |