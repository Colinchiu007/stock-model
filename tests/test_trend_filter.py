"""趋势过滤器测试

覆盖三类约束：
1. 趋势判定是否正确（尤其均线附近/数据不足的边界）
2. 拦截规则是否只拦逆势操作，不误伤震荡市
3. 最长持有豁免是否生效（避免"趋势永不破坏 → 永不卖出"）
"""

import pandas as pd

from stock_model.strategy.trend_filter import (
    TrendFilter,
    TrendFilterConfig,
    TrendState,
)


def make_df(closes: list[float], ma_period: int = 60) -> pd.DataFrame:
    """构造带 ma{period} 列的行情数据"""
    df = pd.DataFrame({"close": closes})
    df["ma" + str(ma_period)] = df["close"].rolling(ma_period, min_periods=1).mean()
    return df


# ==================== 趋势判定 ====================


class TestTrendJudgement:
    def test_strong_up(self):
        # 价格远高于 MA
        closes = [10.0] * 59 + [12.0]
        state = TrendFilter().judge(make_df(closes))
        assert state == TrendState.STRONG_UP

    def test_strong_down(self):
        closes = [10.0] * 59 + [8.0]
        state = TrendFilter().judge(make_df(closes))
        assert state == TrendState.STRONG_DOWN

    def test_sideways(self):
        # 价格贴着均线
        closes = [10.0] * 60
        state = TrendFilter().judge(make_df(closes))
        assert state == TrendState.SIDEWAYS

    def test_unknown_on_short_data(self):
        """数据不足必须返回 UNKNOWN 而不是瞎猜"""
        df = make_df([10.0, 10.1, 10.2])
        assert TrendFilter().judge(df) == TrendState.UNKNOWN

    def test_unknown_on_empty(self):
        assert TrendFilter().judge(pd.DataFrame()) == TrendState.UNKNOWN
        assert TrendFilter().judge(None) == TrendState.UNKNOWN

    def test_deviation_pct(self):
        closes = [10.0] * 59 + [11.0]
        dev = TrendFilter().deviation_pct(make_df(closes))
        assert dev > 0.09, f"偏离度应约 +10%, 实际 {dev:.2%}"

    def test_uses_ma_column_when_available(self):
        """优先使用 technical.py 产出的 ma{period} 列"""
        closes = [10.0] * 59 + [12.0]
        df = make_df(closes)
        assert "ma60" in df.columns
        assert TrendFilter().judge(df) == TrendState.STRONG_UP


# ==================== 拦截规则 ====================


class TestBlockingRules:
    def test_block_sell_in_strong_up(self):
        """核心规则: 强上涨趋势中禁止卖出"""
        df = make_df([10.0] * 59 + [12.0])
        assert TrendFilter().should_block("sell", df) is True

    def test_allow_buy_in_strong_up(self):
        """强上涨中买入应该放行 —— 跟随趋势"""
        df = make_df([10.0] * 59 + [12.0])
        assert TrendFilter().should_block("buy", df) is False

    def test_block_buy_in_strong_down(self):
        """强下跌趋势中禁止买入 —— 不接飞刀"""
        df = make_df([10.0] * 59 + [8.0])
        assert TrendFilter().should_block("buy", df) is True

    def test_allow_sell_in_strong_down(self):
        """强下跌中卖出应该放行"""
        df = make_df([10.0] * 59 + [8.0])
        assert TrendFilter().should_block("sell", df) is False

    def test_sideways_allows_everything(self):
        """横盘市必须完全放行 —— 过滤器不能改变震荡市表现"""
        df = make_df([10.0] * 60)
        tf = TrendFilter()
        assert tf.should_block("sell", df) is False
        assert tf.should_block("buy", df) is False

    def test_unknown_allows_everything(self):
        """数据不足时放行，不凭空拦截"""
        df = make_df([10.0, 10.1])
        tf = TrendFilter()
        assert tf.should_block("sell", df) is False
        assert tf.should_block("buy", df) is False


# ==================== 最长持有豁免 ====================


class TestMaxHoldExemption:
    def test_max_hold_overrides_block(self):
        """超过最长持有期后强制放行卖出"""
        cfg = TrendFilterConfig(max_hold_bars=10)
        tf = TrendFilter(cfg)
        df = make_df([10.0] * 59 + [12.0])  # 强上涨

        assert tf.should_block("sell", df, bars_held=5) is True
        assert tf.should_block("sell", df, bars_held=10) is False
        assert tf.should_block("sell", df, bars_held=50) is False

    def test_no_max_hold_always_blocks(self):
        """max_hold_bars=None 时不豁免"""
        tf = TrendFilter(TrendFilterConfig(max_hold_bars=None))
        df = make_df([10.0] * 59 + [12.0])
        assert tf.should_block("sell", df, bars_held=999) is True


# ==================== 配置 ====================


class TestConfig:
    def test_default_period(self):
        assert TrendFilterConfig().ma_period == 60

    def test_describe_readable(self):
        d = TrendFilterConfig(max_hold_bars=20).describe()
        assert "MA60" in d and "20天" in d

    def test_configurable_thresholds(self):
        """阈值可调: 放宽后同样数据不再判强上涨"""
        loose = TrendFilter(TrendFilterConfig(strong_up_threshold=0.30))
        df = make_df([10.0] * 59 + [12.0])  # 偏离 +20%
        assert loose.judge(df) != TrendState.STRONG_UP


# ==================== 变异防护 ====================


class TestFilterDoesNotOverreach:
    """防止过滤器退化成「什么都不做」或「什么都拦」"""

    def test_filter_actually_blocks_something(self):
        """锁：强上涨中卖出必须被拦 —— 否则趋势过滤器形同虚设"""
        df = make_df([10.0] * 59 + [12.0])
        assert TrendFilter().should_block("sell", df) is True

    def test_filter_is_not_block_everything(self):
        """锁：过滤器不能把所有操作都拦掉"""
        tf = TrendFilter()
        up = make_df([10.0] * 59 + [12.0])
        down = make_df([10.0] * 59 + [8.0])
        assert tf.should_block("buy", up) is False  # 上涨中买
        assert tf.should_block("sell", down) is False  # 下跌中卖

    def test_real_2019_case_shape_is_blocked(self):
        """回归真实案例形态: 上升趋势中的 SELL 必须被拦

        实测 002541 在 2019-04-03（价 10.22 > MA20 9.59）发出 SELL，
        而当日相对 MA60 亦处高位。构造等效形态：价格持续高于 MA60 达 8% 以上。
        """
        # 前 59 天横盘于 10，最后一天跳到 12（偏离 +20%）—— 等效"上涨趋势中想卖"
        closes = [10.0] * 59 + [12.0]
        df = make_df(closes)
        tf = TrendFilter()
        assert tf.judge(df) == TrendState.STRONG_UP
        assert tf.should_block("sell", df) is True

    def test_near_ma_case_is_not_blocked(self):
        """贴近均线时不应拦截 —— 那是震荡市特征，不是趋势

        这条同样重要: 若把"略微高于均线"也拦, 过滤器会退化成
        "永远不卖", 那比不过滤更糟。

        注意: MA60 含当天收盘, 故最后一天的偏离会略大于原始涨幅。
        用 10.10 而非 10.22, 确保落在 SIDEWAYS 区间。
        """
        closes = [10.0] * 59 + [10.10]  # 偏离约 +1%
        df = make_df(closes)
        tf = TrendFilter()
        assert tf.judge(df) == TrendState.SIDEWAYS
        assert tf.should_block("sell", df) is False

    def test_explain_is_readable(self):
        df = make_df([10.0] * 59 + [12.0])
        s = TrendFilter().explain(df, bars_held=3)
        assert "strong_up" in s and "持有3天" in s
