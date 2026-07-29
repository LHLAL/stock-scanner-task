# 多数据源轮询 + 故障切换设计方案

> 现状（精简后）：`stock-scanner-task` 实现腾讯 / 通达信 双路故障切换，
> 任意一路失败自动切换下一路，轮询顺序定期轮换防止单路触发风控。
>
> 历史备注：早期版本（commit `af5e6b3`）含新浪 `SinaSource`（GBK 编码）作为第三路备援，
> commit `0871b90` 因代理（127.0.0.1:7897）下 GBK 解码频繁超时，已主动下线。

---

## 一、架构设计

### 1.1 核心思路

```
StockMenuBarApp._do_fetch(codes)
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│              RotatingMultiFetcher                        │
│                                                          │
│   ┌──────────────────────────────────────────────────┐   │
│   │  CircuitBreaker(successive_fail_limit=3,         │   │
│   │                   cooldown_seconds=60)            │   │
│   └──────────────────────────────────────────────────┘   │
│                                                          │
│   source 注册顺序（按 config.data_sources.enabled 顺序）│
│       默认: [TencentSource, TDXSource]                  │
│                                                          │
│   抓取流程:                                              │
│       1. 从 _index 起尝试各路 Source                     │
│       2. 成功 → 轮换 _index（该 Source 排到队尾）         │
│       3. 失败（网络/解析/超时/ImportError）→ CircuitBreaker│
│          记录；连续失败 3 次后该源进入 60s 冷却期         │
│       4. 所有源失败 → 返回缓存 + 记录 ERROR              │
└──────────────────────────────────────────────────────────┘
```

### 1.2 数据源对比

| Source | 数据格式 | 编码 | 协议 | 限流风险 | 需安装 |
|--------|---------|------|------|---------|--------|
| **TencentSource** | `v_sh600519="1~名称~代码~现价~昨收~..."` | UTF-8 | HTTP `qt.gtimg.cn` | 极低（已有数年稳定记录） | ❌ |
| **TDXSource** | mootdx 二进制解析 | — | TCP 7709 | 无（不通外网） | ✅ `pip install mootdx` |

> 历史 SinaSource：HTTP `hq.sinajs.cn`，GBK 编码。代理环境下 `response.content.decode("gbk")` 频繁超时，已下线。

---

## 二、模块设计

### 2.1 文件结构

```
app/
├── multi_fetcher.py        # 数据源 + 轮询 + 熔断 + 缓存
│   ├── StockQuote (dataclass)        # 行情快照
│   ├── StockSource (Protocol)        # 数据源接口
│   ├── TencentSource                 # 腾讯 HTTP
│   ├── TDXSource                     # 通达信 mootdx
│   ├── CircuitBreaker                # 熔断
│   └── RotatingMultiFetcher          # 轮询 + 熔断 + 缓存
├── config.py              # DataSourceConfig 读取 data_sources 段
├── menu_bar.py            # 构造 sources 列表 → RotatingMultiFetcher
└── storage.py / monitor.py # 消费方（与本设计无关）
```

### 2.2 `StockSource` Protocol

```python
class StockSource(Protocol):
    name: str  # "Tencent" | "TDX"

    def fetch(self, codes: List[str]) -> List[StockQuote]:
        """抓取行情；失败必须抛出异常，不返回空列表。"""
        ...

    def health_check(self) -> bool:
        """快速探测连通性。"""
        ...
```

### 2.3 `TDXSource` 实现要点（mootdx 0.11+）

```python
HAS_MOOTDX = False
try:
    import mootdx
    HAS_MOOTDX = True
except ImportError:
    logger.info("mootdx not installed, TDXSource disabled")

class TDXSource:
    def __init__(self, name_map: Optional[Dict[str, str]] = None, timeout: int = 5):
        self._name_map = name_map or {}
        if not HAS_MOOTDX:
            self._client = None
            return
        try:
            from mootdx.quotes import Quotes
            self._client = Quotes().factory(market="std", timeout=timeout)
        except Exception as e:
            logger.warning(f"[TDXSource] init failed: {e}")
            self._client = None

    def fetch(self, codes):
        if not HAS_MOOTDX or self._client is None:
            raise ImportError("mootdx not installed or TDX init failed")
        df = self._client.quotes(codes)  # pandas DataFrame
        return self._parse_df(df)
```

**mootdx 0.11+ API 变更**（相对旧版）：

| 旧 (0.10-) | 新 (0.11+) |
|-----------|-----------|
| `from mootdx import TDX` | `import mootdx`（探测用） |
| `client = TDX()` | `client = Quotes().factory(market="std", timeout=N)` |
| `client.quotes(['sh600519'])` | `client.quotes(['sh600519'])`（API 兼容；内部自动转换） |
| 返回列 `close` / `settlement` | 返回列 `last_close`（昨收） |
| 返回 `name` 字段 | 返回 `name=None`（需调用方通过 `name_map` 提供） |
| 返回 `code` 带 `sh/sz` 前缀 | 返回 `code` 为纯数字（需代码内补前缀） |

- `mootdx` 是**可选依赖**；未安装或初始化失败时 `HAS_MOOTDX = False` / `_client = None`，`fetch()` 抛 `ImportError` 触发下一路切换
- `name_map` 由 `menu_bar.py` 从 `config.holdings` / `config.indices` 构造，传入以补齐中文名
- 股票代码前缀在 `_parse_df` 用 TDX 的 `market` 列（1=SH / 0=SZ）补齐，比靠首位数字猜更可靠（指数也适用）

**Fallback 服务器列表**：

mootdx 0.11+ 默认走 `BESTIP` 配置，但其 `BESTIP.HQ` 字段默认是空字符串而非空列表，会导致 `ip, port = self.server` 抛 `ValueError: not enough values to unpack`。`TDXSource.__init__` 因此增加了 fallback 探测：

```python
fallback_servers = [
    ("60.191.117.167", 7709),   # 上海电信
    ("180.153.18.170", 7709),   # 上海电信
    ("60.12.136.250", 7709),    # 杭州电信
]
# 先试 None（走默认 discovery），失败按 fallback_servers 顺序探测
```

每次连接立即用 `client.quotes(["600519"])` 做一次轻量探活，确认真的能拉到数据才算初始化成功。

### 2.4 `CircuitBreaker` 设计

```python
class CircuitBreaker:
    def __init__(self, successive_fail_limit=3, cooldown_seconds=60):
        self._fail_count: Dict[str, int] = defaultdict(int)
        self._last_fail_time: Dict[str, float] = {}

    def is_available(self, source_name: str) -> bool:
        if self._fail_count[source_name] < self._successive_fail_limit:
            return True
        return time.time() - self._last_fail_time[source_name] >= self._cooldown_seconds
```

- 成功 → `record_success` 归零计数
- 失败 → `record_failure` 累加 + 记时间戳

### 2.5 `RotatingMultiFetcher` 核心逻辑

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
            except ImportError:
                # TDXSource mootdx 未安装，直接跳过
                self._breaker.record_failure(source.name)
            except Exception:
                self._breaker.record_failure(source.name)

        # 所有源失败，返回缓存
        return [self._cache[c] for c in codes if c in self._cache]
```

---

## 三、配置项设计

### 3.1 `config.json` 字段

```json
{
  "data_sources": {
    "enabled": ["tencent", "tdx"],
    "successive_fail_limit": 3,
    "cooldown_seconds": 60
  }
}
```

> 早期版本曾有 `order` 字段（指定轮换顺序），但实际顺序由 `enabled` 决定 + 内部轮转，**已删除**。

### 3.2 `DataSourceConfig` dataclass

```python
@dataclass
class DataSourceConfig:
    enabled: List[str] = field(default_factory=lambda: ["tencent", "tdx"])
    successive_fail_limit: int = 3
    cooldown_seconds: int = 60
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
┌─ TDXSource ─────────────────────┐
│  try:                            │
│    quotes = tdx.fetch(...)       │
│    ✓ → _index=0, return quotes  │
│  except ImportError:             │
│    # mootdx 未安装，直接跳过     │
│  except:                         │
│    breaker.record_failure()       │
│    → 所有源失败                  │
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
| **TDX 无限制** | 通达信走内网协议，不走外网，零风控风险 |
| **缓存兜底** | 所有源都失败时返回最近一次成功结果，确保监控不中断 |

---

## 六、依赖变更

### `requirements.txt`

```
rumps>=0.4.0
requests>=2.31.0
pyobjc>=10.0
mootdx>=0.7.0   # TDX 数据源依赖（未装则自动跳过该路）
```

- 腾讯一路**无需任何额外依赖**，纯 HTTP 即可
- `mootdx` 不装不影响腾讯一路正常工作（`HAS_MOOTDX = False` → `TDXSource.fetch` 抛 `ImportError` → 跳过）

---

## 七、向后兼容性

| 场景 | 行为 |
|------|------|
| 用户未在 `config.json` 写 `data_sources` | `DataSourceConfig` 用默认值 `["tencent", "tdx"]`，双路全开 |
| mootdx 未安装 | TDXSource 抛 `ImportError`，自动跳过，仅腾讯单路工作 |
| 腾讯接口恢复 | `record_success()` 立即将 `fail_count` 归零，立即恢复 |
| 旧版 `config.json` 残留 `order` 字段 | `data.get("data_sources", {}).get("enabled", ...)` 走默认；`order` 字段被忽略（无害） |

---

## 八、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 轮换触发时机 | 成功抓取后轮换 | 失败不轮换（保持优先级，等恢复） |
| CircuitBreaker 计数 | 按 Source 独立计数 | 一个 Source 失败不影响其他路 |
| 缓存兜底时机 | 所有源都失败 | 避免单路抖动导致放弃正确数据 |
| TDX 端口 | 7709（默认） | 通达信标准行情端口 |
| 所有源失败日志 | `ERROR` 级别 | 需要人工关注（可能网络整体故障） |
| SinaSource 处置 | 已删除 | 代理环境下 GBK 解码频繁超时，commit `0871b90` |
