"""
股票分析演示脚本 (一期 + 二期功能)

使用示例:
    # 安装依赖
    pip install -e ".[dev,quant]"

    # 运行演示
    python examples/demo.py
"""

import numpy as np
import pandas as pd

from stock_model.config.settings import get_settings
from stock_model.data.fetcher import StockDataFetcher
from stock_model.data.processor import DataProcessor
from stock_model.data.monitor import DataQualityMonitor
from stock_model.analysis.technical import TechnicalAnalysis
from stock_model.analysis.fundamental import FundamentalAnalysis
from stock_model.analysis.signals import SignalGenerator
from stock_model.visualization.charts import ChartBuilder
from stock_model.strategy.manual import ManualStrategy
from stock_model.strategy.engine import BacktestEngine, StrategyEngine
from stock_model.risk import RiskManager, PositionSizer
from stock_model.portfolio import PortfolioOptimizer
from stock_model.notify import SignalNotifier
from stock_model.notify.channels import ConsoleChannel
from stock_model.utils.logger import setup_logger


def main():
    # 1. 初始化
    setup_logger()
    settings = get_settings()

    # 2. 创建各模块实例
    fetcher = StockDataFetcher()
    processor = DataProcessor()
    ta = TechnicalAnalysis()
    signal_gen = SignalGenerator()
    strategy = ManualStrategy()

    # 3. 获取数据 (以平安银行为例)
    symbol = "000001"
    print(f"\n{'='*60}")
    print(f"  股票分析演示 - {symbol}")
    print(f"{'='*60}")

    # === 一期功能 ===

    # 3.1 获取日线数据
    print(f"\n[1] 获取 {symbol} 日线数据...")
    df = fetcher.get_daily(symbol, start_date="20240101", end_date="20241231")
    if df is not None and not df.empty:
        print(f"    数据量: {len(df)} 条")
        print(f"    最新收盘价: {df['close'].iloc[-1]:.2f}")
    else:
        print("    无法获取数据，使用模拟数据演示")
        dates = pd.date_range("2024-01-01", periods=120, freq="D")
        np.random.seed(42)
        close = 10 + np.cumsum(np.random.normal(0, 0.2, 120))
        df = pd.DataFrame({
            "open": close + np.random.normal(0, 0.1, 120),
            "high": close + abs(np.random.normal(0, 0.2, 120)),
            "low": close - abs(np.random.normal(0, 0.2, 120)),
            "close": close,
            "volume": np.random.randint(100000, 500000, 120).astype(float),
        }, index=dates)

    # 3.2 数据处理
    print(f"\n[2] 数据处理...")
    df = processor.clean(df)
    df = processor.calculate_returns(df)

    # 3.3 技术分析
    print(f"\n[3] 技术分析...")
    df = ta.analyze_all(df)

    # 3.4 信号生成
    print(f"\n[4] 交易信号...")
    signals = signal_gen.generate_all(df, symbol)
    for s in signals[:5]:
        print(f"    {s}")

    # 3.5 策略分析
    print(f"\n[5] 策略分析...")
    result = strategy.run(symbol, df)
    print(f"    操作建议: {result.action.value}")
    print(f"    信心度: {result.confidence:.2f}")
    print(f"    原因: {result.reason}")

    # === 二期功能 ===

    # 4. 数据质量监控
    print(f"\n{'='*60}")
    print("  二期功能演示")
    print(f"{'='*60}")

    print(f"\n[6] 数据质量监控...")
    monitor = DataQualityMonitor()
    report = monitor.check(df, symbol)
    print(f"    质量评分: {report.score}/100")
    if report.issues:
        for issue in report.issues[:3]:
            print(f"    - [{issue.severity}] {issue.message}")
    else:
        print("    数据质量良好，无问题")

    # 5. 回测引擎
    print(f"\n[7] 回测引擎...")
    backtest = BacktestEngine(initial_cash=100000)
    bt_result = backtest.run(ManualStrategy(), df, symbol)
    metrics = bt_result.metrics
    print(f"    总收益率: {metrics.get('total_return', 0):.2%}")
    print(f"    年化收益率: {metrics.get('annual_return', 0):.2%}")
    print(f"    夏普比率: {metrics.get('sharpe', 0):.2f}")
    print(f"    最大回撤: {metrics.get('max_drawdown', 0):.2%}")
    print(f"    交易笔数: {metrics.get('total_trades', 0)}")

    # 6. 风险管理
    print(f"\n[8] 风险管理...")
    risk_mgr = RiskManager(max_drawdown_limit=0.15, position_concentration_limit=0.3)
    from stock_model.risk.models import Position
    positions = [
        Position(symbol="000001", shares=1000, cost_price=10.0, current_price=df["close"].iloc[-1]),
    ]
    alerts = risk_mgr.check_portfolio_risk(positions, total_value=100000)
    if alerts:
        for alert in alerts[:3]:
            print(f"    [{alert.level.value}] {alert.type.value}: {alert.message}")
    else:
        print("    无风险警报")

    # 7. 仓位管理
    print(f"\n[9] 仓位管理...")
    sizer = PositionSizer()
    kelly = sizer.kelly_size(capital=100000, price=df["close"].iloc[-1], win_rate=0.55, avg_win=0.05, avg_loss=0.025)
    print(f"    凯利公式建议仓位: {kelly:.0f} 股")
    fixed = sizer.fixed_size(capital=100000, price=df["close"].iloc[-1], position_pct=0.1)
    print(f"    固定比例(10%)建议: {fixed:.0f} 股")

    # 8. 组合优化
    print(f"\n[10] 组合优化...")
    optimizer = PortfolioOptimizer()
    prices = {"000001": 10.0, "000002": 20.0, "600036": 30.0}
    portfolio = optimizer.equal_weight(
        symbols=list(prices.keys()), prices=prices, total_value=100000
    )
    print(f"    等权重组合: {portfolio.name}")
    for w in portfolio.weights:
        print(f"      {w.symbol}: 权重={w.weight:.2%}, 股数={w.shares}")

    # 9. 信号推送
    print(f"\n[11] 信号推送...")
    notifier = SignalNotifier()
    notifier.add_channel(ConsoleChannel())
    notifier.notify(result)

    print(f"\n{'='*60}")
    print("  分析完成!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()