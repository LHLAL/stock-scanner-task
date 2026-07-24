# A股持仓监控

macOS 状态栏实时看盘工具。在菜单栏显示 A 股持仓股的实时价格，红色上涨，绿色下跌，异动时弹出系统通知。

## 功能特性

- **状态栏实时显示**：上涨/下跌股票数一目了然
- **中国股市配色**：上涨 = 红色，下跌 = 绿色（通过 NSAttributedString 实现）
- **每 5 秒轮询**：腾讯行情 API，无需注册，无需 Token
- **阈值告警**：涨跌幅超过设定值时弹出系统通知
- **异动检测**：对比近 3 轮数据，检测股价异常波动
- **菜单展开时持续刷新**：通过 NSRunLoopCommonModes 保证菜单打开时定时器正常运行
- **后台运行**：支持 start/stop/restart/status 进程管理
- **展开子菜单**：查看每只股票的现价、昨收、涨跌额、涨跌幅详情

## 技术栈

- Python 3 + rumps（macOS 状态栏框架）
- requests + pyobjc
- 腾讯行情 API（qt.gtimg.cn）

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
| `./run.sh` | 启动（默认） |
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
  "poll_interval_seconds": 5
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
| `sudden_threshold_pct` | 异动检测阈值（与近3轮均值的偏差） | 1.0 |
| `poll_interval_seconds` | 轮询间隔（秒） | 5 |

### 持仓字段说明

| 字段 | 说明 | 必填 |
|------|------|------|
| `code` | 股票代码（sh/sz 前缀） | 是 |
| `name` | 股票名称 | 是 |
| `cost` | 持仓成本价 | 否（不填则只看盘） |
| `shares` | 持仓股数 | 否 |
| `stop_loss` | 独立止损线（覆盖全局） | 否 |
| `take_profit` | 独立止盈线（覆盖全局） | 否 |

### 股票代码格式

| 前缀 | 交易所 | 代码范围 |
|------|--------|----------|
| `sh` | 上海证券交易所 | 600xxx, 601xxx, 603xxx |
| `sz` | 深圳证券交易所 | 000xxx, 001xxx, 002xxx, 300xxx |

> 注意：`002` 开头的中小板和 `300` 开头的创业板属于深圳交易所，必须使用 `sz` 前缀。

## 项目结构

```
stock-scanner-task/
├── config.json          # 持仓股配置
├── stock-monitor.log    # 运行日志（自动生成）
├── README.md            # 本文件
├── run.sh               # 终端启动/停止脚本
├── start.command        # Finder 双击启动脚本
├── stock_monitor.py     # 主入口
└── app/
    ├── __init__.py
    ├── config.py        # 配置加载
    ├── fetcher.py       # 腾讯行情 API 封装
    ├── monitor.py       # 价格监控 + 异动检测
    └── menu_bar.py      # macOS 状态栏 UI
```

## 日志

运行日志输出到项目目录下的 `stock-monitor.log`：

```bash
tail -f stock-monitor.log
```

## 常见问题

**Q: 股票查不到？**  
检查股票代码前缀是否正确。`002` 和 `300` 开头是深圳股票，必须用 `sz` 前缀。

**Q: 菜单打开时数据不刷新？**  
已修复。定时器注册在 NSRunLoopCommonModes 下，菜单展开时仍然正常触发。
