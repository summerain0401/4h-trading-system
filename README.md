# BTC 4H 波段交易系统 V9

基于 Hyperliquid 的半自动 BTC 趋势跟踪交易系统，使用 4 小时 K 线进行波段操作。四阶段信号引擎覆盖完整的趋势生命周期：方向确认 → 金叉入场 → 加速追仓 → 回调加仓。

## 核心策略

| 阶段 | 名称 | 操作 | 说明 |
|------|------|------|------|
| **Phase 1** | 方向确认 | 判定趋势方向 | EMA 排列 + ADX 过滤 + K 线形态确认，决定 LONG/SHORT |
| **Phase 2** | 金叉入场 | 首次建仓 | 5EMA 上穿 20VWMA (LONG) 或下穿 (SHORT)，最近 3 根 K 线入场 |
| **Phase 3** | 趋势加速 | SHORT 追仓 | 趋势加速形成的追仓机会，仅做空 |
| **Phase 4** | 回调加仓 | 已有仓位=加仓 / 无仓位=独立入场 | 价格回调至 20VWMA 附近，半仓独立入场 |

### 风险控制

- **非对称多空参数**：LONG/SHORT 使用不同的止损宽度、跟踪缓冲、超时设置
- **三层利润保护**：盈利 <5% 正常跟踪 → 5-10% 收紧至 9EMA+0.5ATR → >10% 锁仓至 9EMA
- **仓位计算**：`仓位 = 最大亏损 / |入场价 - 止损价|`，基于风险优先
- **冷却机制**：出场后最少 3 根 K 线冷却；连续两次 >2% 亏损触发 8 根 K 线熔断
- **震荡市过滤**：ADX < 20 + BB/DI 收敛 + Choppiness Index > 65，三重条件独立判断

## 快速开始

### 环境要求

- Python 3.9+
- macOS / Linux

### 安装

```bash
# 克隆仓库
git clone git@github.com:summerain0401/4h-trading-system.git
cd 4h-trading-system

# 创建虚拟环境并安装依赖
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 配置

创建 `.env` 文件，设置 Hyperliquid API 密钥：

```env
PRIVATE_KEY=your_wallet_private_key
HYPERLIQUID_WALLET_ADDRESS=your_wallet_address
```

> 不设置密钥时，系统以只读模式运行（仅生成信号，不下单）。

交易参数在 `config.yaml` 中配置，包括杠杆、止损 ATR 倍数、多空方向参数等。

### 使用

```bash
# 交互式 CLI 菜单
python main.py

# 直接进入监控模式（每 5 分钟检查一次）
python main.py monitor

# 快速查看市场状态
python main.py status

# Web 仪表盘（FastAPI，端口 8080）
python dashboard.py

# 回测（灵活日期范围）
python verify_signals.py                        # 默认：近 90 天主网数据
python verify_signals.py --days 180             # 近 180 天
python verify_signals.py --start 2025-01-01 --end 2025-06-30  # 指定日期
```

## 项目结构

```
main.py                  # CLI 入口，TradingSystem 编排器
dashboard.py             # FastAPI + 服务端渲染仪表盘 (端口 8080)
verify_signals.py        # 回测脚本，支持灵活日期范围
config.yaml              # 交易参数配置

core/
  signal_engine.py       # 核心 — 四阶段信号检测 + 止损/止盈
  indicators.py          # 纯函数指标 (EMA, VWMA, ADX, ATR, BB, DI, CI)
  patterns.py            # K 线形态识别
  risk_manager.py        # 仓位计算、止损价计算
  order_executor.py      # 信号 → 交易所订单转换
  data_fetcher.py        # Hyperliquid OHLCV 数据拉取
  param_optimizer.py     # 网格搜索参数优化

exchange/
  hl_client.py           # Hyperliquid 对接 (REST + Python SDK)

storage/
  models.py              # 数据类 (Kline, Signal, Position, MarketState)
  db.py                  # SQLite CRUD

notify/
  terminal.py            # 终端彩色信号卡片输出
  desktop.py             # macOS 通知中心提醒

templates/
  dashboard.html         # 仪表盘前端页面
```

## 技术要点

- **时区**：所有时间戳使用 **UTC+8（北京时间）**，定义在 `storage/models.py` 的 `BEIJING_TZ`
- **信号引擎**：纯函数设计，接收 DataFrame 返回 Signal 对象，无副作用
- **数据源**：Hyperliquid 主网，4H 为主周期，1D 为大周期辅助
- **参数优化同步**：代码修改后需同步更新 `4H波段交易手册.md`

## 测试

```bash
pytest tests/ -v
```

## 免责声明

本系统仅供研究和教育目的使用。加密货币交易具有高风险，过往表现不代表未来收益。使用本软件进行的任何交易决策均由用户自行承担责任。

## License

MIT
