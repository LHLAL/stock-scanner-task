# 多数据源轮询 + 故障切换设计方案

> 目标：在 `stock-scanner-task` 中实现腾讯 / 新浪 / 通达信 三路并行故障切换，
> 任意一路失败自动切换下一路，轮询顺序定期轮换防止单路触发风控。

---

## 一、架构设计

### 1.1 核心思路

```
StockFetcher.fetch(codes)
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│              RotatingMultiSourceFetcher                   │
│                                                          │
│   ┌──────────────────────────────────────────────────┐   │
│   │  CircuitBreaker(successive_fail_limit=3,         │   │
│   │                   cooldown_seconds=60)            │   │
│   └──────────────────────────────────────────────────┘   │
│                                                          │
│   source 轮换顺序（每次成功抓取后轮换）:                  │
│       轮换队列: [TencentSource, SinaSource, TDXSource] │
│                                                          │
│   抓取流程:                                              │
│       1. 尝试当前优先级的 Source                         │
│       2. 成功 → 更新轮换顺序（该 Source 排到队尾）        │
│       3. 失败（网络/解析/超时）→ CircuitBreaker 检查    │
│          通过则尝试下一路 Source                         │
│       4. 三路全失败 → 返回缓存 + 记录 ERROR              │
└──────────────────────────────────────────────────────────┘
```

### 1.2 三路数据源对比

| Source | 数据格式 | 编码 | 协议 | 限流风险 | 需安装 |
|--------|---------|------|------|---------|--------|
| **TencentSource** | `v_sh600519="1~名称~代码~现价~昨收~..."` | UTF-8 | HTTP | 极低（已有数年稳定记录） | ❌ |
| **SinaSource** | `hq_str_sh600519="名称,代码,现价,...,..."` | GBK | HTTP | 低 | ❌ |
| **TDXSource** | mootdx 二进制解析 | — | TCP 7709 | 无（不通外网） | ✅ `pip install mootdx` |

---

## 二、模块设计

### 2.1 文件结构

```
app/
├── fetcher.py              # 保留（TencentSource 从中拆分，行为完全一致）
├── multi_fetcher.py        # 新增：多源轮询封装
│   ├── StockSource (protocol)
│   ├── TencentSource       # 封装现有逻辑
│   ├── SinaSource         # 新浪行情
│   ├── TDXSource          # 通达信（mootdx）
│   ├── CircuitBreaker
│   └── RotatingMultiFetcher # 轮询 + 熔断 + 缓存
├── config.py              # 小改：增加 data_sources 配置
└── menu_bar.py            # 改动：StockFetcher → RotatingMultiFetcher
```

### 2.2 `StockSource` Protocol

```python
class StockSource(Protocol):
    name: str  # "Tencent" | "Sina" | "TDX"

    def fetch(self, codes: List[str]) -> List[StockQuote]:
        """抓取行情；失败抛出异常，不返回空列表"""
        ...

    def health_check(self) -> bool:
        """快速探测连通性"""
        ...
```

### 2.3 `SinaSource` 实现要点

新浪接口格式（需 GBK 解码）：

```
GET http://hq.sinajs.cn/list=sh600519,sz000001
Response (GBK):
var hq_str_sh600519="贵州茅台,600519,1322.00,1253.00,...";
var hq_str_sz000001="平安银行,000001,12.34,12.00,...";

字段（逗号分隔）:
[0]  名称    [1]  代码    [2]  今开    [3]  昨收
[4]  当前价  [5-9] 买1-5价  [10-14] 卖1-5价
[15] 现量    [16] 成交量（股）[17] 最高  [18] 最低  [19] 时间
```

- 响应是 GBK 编码，需 `response.content.decode('gbk')`
- 股票前缀 `sh` / `sz` 出现在变量名中，不是字段里
- 解析：`hq_str_sh600519` → 去掉 `hq_str_` 得到 `sh600519`

### 2.4 `TDXSource` 实现要点

```python
HAS_MOOTDX = False
try:
    from mootdx import TDX
    HAS_MOOTDX = True
except ImportError:
    logger.info("mootdx not installed, TDXSource disabled")

class TDXSource:
    def __init__(self):
        self._client = TDX() if HAS_MOOTDX else None

    def fetch(self, codes):
        if not HAS_MOOTDX:
            raise ImportError("mootdx not installed")
        df = self._client.quotes(codes)  # pandas DataFrame
        return self._parse_df(df, codes)
```

- `mootdx` 是**可选依赖**，`HAS_MOOTDX = False` 时直接抛 `ImportError`，触发下一路切换
- mootdx 返回 pandas DataFrame，转换为 `List[StockQuote]`

### 2.5 `CircuitBreaker` 设计

```python
class CircuitBreaker:
    def __init__(self, successive_fail_limit=3, cooldown_seconds=60):
        self._fail_count: Dict[str, int] = defaultdict(int)
        self._last_fail_time: Dict[str, float] = {}
        self._successive_fail_limit = successive_fail_limit
        self._cooldown_seconds = cooldown_seconds

    def is_available(self, source_name: str) -> bool:
        if self._fail_count[source_name] < self._successive_fail_limit:
            return True
        elapsed = time.time() - self._last_fail_time[source_name]
        return elapsed >= self._cooldown_seconds

    def record_success(self, source_name: str) -> None:
        self._fail_count[source_name] = 0

    def record_failure(self, source_name: str) -> None:
        self._fail_count[source_name] += 1
        self._last_fail_time[source_name] = time.time()
```

### 2.6 `RotatingMultiFetcher` 核心逻辑

```python
class RotatingMultiFetcher:
    def fetch(self, codes):
        for offset in range(len(self._sources)):
            idx = (self._index + offset) % len(self._sources)
            source = self._sources[idx]

            if not self._breaker.is_available(source.name):
                continue  # 熔断中，跳过

            try:
                quotes = source.fetch(codes)
                if quotes:
                    self._index = (idx + 1) % len(self._sources)
                    self._breaker.record_success(source.name)
                    self._update_cache(quotes)
                    return quotes
            except Exception:
                self._breaker.record_failure(source.name)
                continue

        # 三路全失败，返回缓存
        return [self._cache[c] for c in codes if c in self._cache]
```

---

## 三、配置项设计

### 3.1 `config.json` 新增字段

```json
{
  "data_sources": {
    "enabled": ["tencent", "sina", "tdx"],
    "order": ["tencent", "sina", "tdx"],
    "successive_fail_limit": 3,
    "cooldown_seconds": 60
  }
}
```

### 3.2 `AppConfig` 扩展

```python
@dataclass
class DataSourceConfig:
    enabled: List[str] = field(
        default_factory=lambda: ["tencent", "sina", "tdx"]
    )
    order: List[str] = field(
        default_factory=lambda: ["tencent", "sina", "tdx"]
    )
    successive_fail_limit: int = 3
    cooldown_seconds: int = 60

@dataclass
class AppConfig:
    # ... 现有字段 ...
    data_sources: DataSourceConfig = field(default_factory=DataSourceConfig)
```

---

## 四、故障切换流程

```
fetch("sh600519,sz000001")
         │
         ▼
┌─ TencentSource ─────────────────┐
│  try:                            │
│    quotes = tencent.fetch(...)    │
│    ✓ → _index=1, return quotes  │
│  except:                         │
│    breaker.record_failure()       │
│    → 尝试下一路                  │
└──────────────────────────────────┘
         │
         ▼（Tencent 失败时）
┌─ SinaSource ────────────────────┐
│  try:                            │
│    quotes = sina.fetch(...)      │
│    ✓ → _index=2, return quotes  │
│  except:                         │
│    breaker.record_failure()       │
│    → 尝试下一路                  │
└──────────────────────────────────┘
         │
         ▼（Sina 也失败时）
┌─ TDXSource ─────────────────────┐
│  try:                            │
│    quotes = tdx.fetch(...)       │
│    ✓ → _index=0, return quotes  │
│  except:                         │
│    breaker.record_failure()       │
│    → 三路全失败                  │
└──────────────────────────────────┘
         │
         ▼
  return cached quotes
  (ERROR 日志)
```

---

## 五、防风控策略

| 策略 | 说明 |
|------|------|
| **轮换顺序** | 每次成功抓取后将刚成功的 Source 排到队尾，避免单一来源高频访问 |
| **CircuitBreaker** | 连续失败 3 次 → 该路进入 60 秒冷却期，不立即重试 |
| **Sina 宽松限流** | 新浪建议 300ms 间隔，走轮换机制后实际调用频率更低 |
| **TDX 无限制** | 通达信走内网协议，不走外网，零风控风险 |
| **缓存兜底** | 三路全失败时返回最近一次成功结果，确保监控不中断 |

---

## 六、依赖变更

### `requirements.txt`

```
mootdx>=0.7.0    # 可选依赖，不装则 TDXSource 自动降级
```

- 腾讯 / 新浪两路**无需任何额外依赖**，纯 HTTP 即可
- `mootdx` 不装不影响另外两路正常工作

---

## 七、向后兼容性

| 场景 | 行为 |
|------|------|
| 用户未更新 `config.json` | `DataSourceConfig` 使用默认值 `["tencent", "sina", "tdx"]`，三路全开 |
| mootdx 未安装 | TDXSource 抛出 `ImportError`，自动跳过，Tencent + Sina 两路工作 |
| 腾讯接口恢复 | `record_success()` 立即将 `fail_count` 归零，立即恢复 |
| 旧版 `config.json` | `data.get("data_sources", {})` 为空字典，走默认值 |

---

## 八、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 轮换触发时机 | 成功抓取后轮换 | 失败不轮换（保持优先级，等恢复） |
| CircuitBreaker 计数 | 按 Source 独立计数 | 一个 Source 失败不影响其他两路 |
| 缓存兜底时机 | 三路全失败 | 避免单路抖动导致放弃正确数据 |
| Sina 编码 | GBK | 新浪财经历史上默认 GBK 编码 |
| TDX 端口 | 7709（默认） | 通达信标准行情端口 |
| 三路全失败日志 | `ERROR` 级别 | 需要人工关注（可能网络整体故障） |
