"""
股票分析演示脚本

使用示例:
    # 安装依赖
    pip install -e ".[dev]"

    # 运行演示
    python examples/demo.py
"""

from stock_model.config.settings import get_settings
from stock_model.data.fetcher import StockDataFetcher
from stock_model.data.processor import DataProcessor
from stock_model.analysis.technical import TechnicalAnalysis
from stock_model.analysis.fundamental import FundamentalAnalysis
from stock_model.analysis.signals import SignalGenerator, SignalType
from stock_model.visualization.charts import ChartBuilder
from stock_model.strategy.manual import ManualStrategy
from stock_model.utils.logger import setup_logger


def main():
    # 1. 初始化
    setup_logger()
    settings = get_settings()

    # 2. 创建各模块实例
    fetcher = StockDataFetcher()
    processor = DataProcessor()
    ta = TechnicalAnalysis()
    fundamental = FundamentalAnalysis()
    signal_gen = SignalGenerator()
    chart_builder = ChartBuilder()
    strategy = ManualStrategy()

    # 3. 获取数据 (以平安银行为例)
    symbol = "000001"
    print(f"\n{'='*60}")
    print(f"  股票分析演示 - {symbol}")
    print(f"{'='*60}")

    # 3.1 获取日线数据
    print(f"\n[1] 获取 {symbol} 日线数据...")
    df = fetcher.get_daily(symbol, start_date="20240101", end_date="20241231")
    print(f"    数据量: {len(df)} 条")
    print(f"    最新数据:\n{df.tail()}\n")

    # 3.2 数据处理
    print(f"[2] 数据处理...")
    df = processor.clean(df)
    df = processor.calculate_returns(df)
    print(f"    处理后数据列: {df.columns.tolist()}\n")

    # 3.3 技术分析
    print(f"[3] 技术分析...")
    df = ta.analyze_all(df)
    print(f"    技术指标列: {[c for c in df.columns if c not in ['open', 'high', 'low', 'close', 'volume', 'amount', 'symbol']]}")
    print(f"    最新技术指标:\n{df.iloc[-1][['close', 'ma5', 'ma20', 'ma60']].to_dict()}\n")

    # 3.4 信号生成
    print(f"[4] 交易信号...")
    signals = signal_gen.generate_all(df, symbol)
    for s in signals:
        print(f"    {s}")
    if not signals:
        print("    当前无交易信号")
    print()

    # 3.5 策略分析
    print(f"[5] 策略分析...")
    result = strategy.run(symbol, df)
    print(f"    操作建议: {result.action.value}")
    print(f"    信心度: {result.confidence:.2f}")
    print(f"    原因: {result.reason}")
    print(f"    建议仓位: {result.position_pct:.1f}%")
    if result.target_price:
        print(f"    目标价: {result.target_price:.2f}")
    if result.stop_loss:
        print(f"    止损价: {result.stop_loss:.2f}")
    print()

    # 3.6 绘制K线图
    print(f"[6] 绘制K线图...")
    fig = chart_builder.kline(df, symbol=symbol, show_volume=True, show_ma=True)
    output_path = settings.data.processed_data_dir / f"{symbol}_kline.html"
    chart_builder.export(fig, output_path)
    print(f"    K线图已保存: {output_path}\n")

    print(f"{'='*60}")
    print("  分析完成!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()