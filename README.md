# BTC 4H 波段交易系统

[中文](README.md) | [English](README.en.md)

基于 Hyperliquid 的 BTC 趋势跟踪研究与半自动交易工具，使用 4 小时 K 线及日线辅助判断，提供终端信号、历史回测和 Web 仪表盘。

**运行边界：** 未提供私钥时只读取行情与生成信号；同时配置 API 钱包私钥和主钱包地址后，监控循环可以自动提交真实订单。网络由 `config.yaml` 决定，不应把监控模式理解为模拟交易。

## 策略与实现

方向确认 → 金叉入场 → SHORT 趋势加速 → VWMA 回调加仓 / 独立入场。包含多空非对称参数、分批止损、跟踪退出、冷却及震荡过滤。规则和历史实验见 [4H 波段交易手册](4H波段交易手册.md)；历史收益不是今天的复核结果，也不代表未来收益。

`core/backtester.py` 为共享回测核心；`verify_signals.py` 做区间回测，`walkforward.py` 做滚动样本外比较，`exit_experiment.py` 做退出参数实验。自适应参数优化默认关闭，策略修改需同步手册。

## 安装与只读起步

源码使用 Python 3.10+ 语法；建议在 macOS / Linux 的独立虚拟环境中运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py status
```

以上命令读取网络行情并初始化本地 SQLite。首次运行前，确认当前终端没有继承 `PRIVATE_KEY` 或 `HYPERLIQUID_PRIVATE_KEY`。

```bash
python main.py             # 交互式菜单
python main.py monitor     # 持续监控；配置密钥后可自动下单
python verify_signals.py --days 90
python walkforward.py --days 240 --interval 15 --mode all
```

当前 CLI 从**进程环境变量**读取配置，不会自动加载 `.env` 文件。私钥名称为 `PRIVATE_KEY` 或 `HYPERLIQUID_PRIVATE_KEY`；主钱包地址为 `HYPERLIQUID_WALLET_ADDRESS` 或 `HYPERLIQUID_MAIN_ADDRESS`。请自行安全注入，勿提交或粘贴真实密钥到文档中。

## 可选 Web 仪表盘

```bash
pip install fastapi uvicorn
python dashboard.py
```

默认端口 8080，绑定 `0.0.0.0`。FastAPI / Uvicorn 尚未列入 `requirements.txt`；仪表盘读取的私钥变量为 `HYPERLIQUID_PRIVATE_KEY`，钱包地址为 `HYPERLIQUID_WALLET_ADDRESS`。

## 目录与已知限制

- `core/`：信号、指标、仓位风险、回测及订单执行。
- `exchange/`：行情 REST 与 Hyperliquid SDK。
- `storage/`：数据模型及 SQLite；时间口径为 UTC+8。
- `notify/`、`templates/`：通知与仪表盘视图。

`tests/` 当前没有测试用例，不能把 `pytest tests/ -v` 视作已有测试覆盖。交互菜单的持仓/手动下单项仍含未完成逻辑，不属于已经验证的功能。本次整理未执行联网回测或真实交易。

## 风险与许可

仅供研究；加密资产交易可能造成损失。原 README 标注 MIT，但默认分支尚无独立 LICENSE 文件，本次未补设或更换项目许可证。
