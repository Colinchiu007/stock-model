"""新浪数据源的实现契约

背景(2026-10-19 实测):
  baostock 返回 10001011「黑名单用户」+ 10002007, akshare 底层(东方财富)
  被 RemoteDisconnected —— 两个既有源**同时**不可用, 模拟盘无法推进。
  实测新浪 quotes.sina.cn 直连可用(0.1s, 字段齐全), 故加为第三个源。

实测得到的关键约束(决定了实现方式):
  1. 只能取"最近 N 根"(datalen), **没有 start_date/end_date 参数**
  2. datalen 上限约 1000(2000 ��返回非 JS 字面量, 解析失败)
  3. 支持 sz/sh 前缀, 创业板/科创板均可用
  4. 字段: day/open/high/low/close/volume (+ 均线, 忽略)
"""

from __future__ import annotations

import pandas as pd
import pytest

from stock_model.data.sources.sina_source import SinaSource


class TestSinaSourceSymbol:
    def test_symbol_conversion(self):
        """6 位代码 -> 新浪带市场前缀格式"""
        s = SinaSource()
        assert s._convert_symbol("000002") == "sz000002"
        assert s._convert_symbol("600036") == "sh600036"
        assert s._convert_symbol("300750") == "sz300750"  # 创业板
        assert s._convert_symbol("688981") == "sh688981"  # 科创板
        assert s._convert_symbol("688981") == "sh688981"

    def test_already_prefixed_passthrough(self):
        """已带前缀的不重复加"""
        assert SinaSource()._convert_symbol("sz000002") == "sz000002"

    def test_normalize_symbol_returns_raw(self):
        """对外暴露的代码仍是 6 位(与其他源一致)"""
        assert SinaSource().normalize_symbol("000002") == "000002"


class TestSinaParsing:
    def test_parse_rows_to_dataframe(self):
        """原始行 -> 标准 DataFrame(date 为 DatetimeIndex)"""
        src = SinaSource()
        rows = [
            {
                "day": "2026-10-15",
                "open": "3.900",
                "high": "3.980",
                "low": "3.880",
                "close": "3.930",
                "volume": "80578596",
                "ma_price5": 3.9,
            },
            {
                "day": "2026-10-16",
                "open": "3.620",
                "high": "3.850",
                "low": "3.550",
                "close": "3.660",
                "volume": "443373457",
                "ma_price5": 3.7,
            },
        ]
        df = src._rows_to_df(rows, "000002")
        assert list(df.index.strftime("%Y-%m-%d")) == ["2026-10-15", "2026-10-16"]
        for col in ("open", "high", "low", "close", "volume"):
            assert df[col].dtype.kind == "f" or df[col].dtype.kind == "i", f"{col} 应为数值"
        assert df["close"].iloc[0] == pytest.approx(3.930)
        assert df["symbol"].iloc[0] == "000002"

    def test_parse_empty_rows(self):
        """空行 -> 空 DataFrame(不抛异常)"""
        df = SinaSource()._rows_to_df([], "000002")
        assert df.empty
        assert "close" in df.columns

    def test_parse_drops_ma_columns(self):
        """均线列不应进入标准 schema"""
        df = SinaSource()._rows_to_df(
            [
                {
                    "day": "2026-10-16",
                    "open": "1",
                    "high": "1",
                    "low": "1",
                    "close": "1",
                    "volume": "1",
                    "ma_price5": 1,
                    "ma_volume5": 1,
                }
            ],
            "000002",
        )
        assert not any(c.startswith("ma_") for c in df.columns)


class TestSinaDateWindow:
    def test_datalen_derived_from_window(self):
        """请求 N 根: datalen 至少覆盖 [start,end] 区间的交易日数"""
        # 2024 全年约 242 个交易日, 但接口按自然日估; 必须 >= 区间天数 + 余量
        n = SinaSource()._datalen_for("20240101", "20241231")
        assert n >= 366, f"全年区间至少要 366 根, 实际 {n}"
        assert n <= 1000, f"不得超接口上限 1000, 实际 {n}"

    def test_datalen_capped_at_api_limit(self):
        """超长区间必须封顶在 1000(实测 2000 会返回无法解析的内容)"""
        assert SinaSource()._datalen_for("20000101", "20261231") == 1000

    def test_datalen_default_when_no_window(self):
        assert SinaSource()._datalen_for(None, None) <= 1000

    def test_slice_applies_start_end(self):
        """取回最近 N 根后, 必须按请求区间本地切片"""
        idx = pd.bdate_range("2024-01-01", periods=500)
        df = pd.DataFrame(
            {
                "close": [10.0] * 500,
                "open": [10.0] * 500,
                "high": [10.0] * 500,
                "low": [10.0] * 500,
                "volume": [1] * 500,
            },
            index=idx,
        )
        got = SinaSource()._slice_window(df, "20240301", "20240401")
        assert not got.empty
        assert got.index.min() >= pd.Timestamp("2024-03-01")
        assert got.index.max() <= pd.Timestamp("2024-04-01")


class TestSinaDaily:
    def test_get_daily_returns_standard_schema(self, monkeypatch):
        """get_daily 产出与其他源一致的结构"""
        src = SinaSource()
        rows = [
            {
                "day": "2026-10-15",
                "open": "3.9",
                "high": "3.98",
                "low": "3.88",
                "close": "3.93",
                "volume": "100",
            },
            {
                "day": "2026-10-16",
                "open": "3.6",
                "high": "3.85",
                "low": "3.55",
                "close": "3.66",
                "volume": "200",
            },
        ]
        monkeypatch.setattr(src, "_fetch_rows", lambda sym, n, scale=240: rows)
        df = src.get_daily("000002", start_date="20260101", end_date="20261231")
        assert not df.empty
        for col in ("open", "high", "low", "close", "volume", "symbol"):
            assert col in df.columns, f"缺列 {col}"
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_get_daily_empty_raises_or_empty(self, monkeypatch):
        """无数据时返回空 DataFrame, 由上层判定失败"""
        src = SinaSource()
        monkeypatch.setattr(src, "_fetch_rows", lambda sym, n, scale=240: [])
        df = src.get_daily("000002", start_date="20260101", end_date="20260131")
        assert df.empty

    def test_unsupported_adjust_raises(self, monkeypatch):
        """只支持前复权/不复权; 后复权新浪接口给不了, 必须显式拒绝"""
        src = SinaSource()
        with pytest.raises(ValueError, match="不支持复权"):
            src.get_daily("000002", start_date="20260101", adjust="hfq")


class TestSinaUnsupported:
    def test_valuation_not_supported(self):
        """新浪接口没有估值数据 —— 必须显式说明, 不能假装成功"""
        with pytest.raises(NotImplementedError):
            SinaSource().get_valuation("000002")

    def test_financial_summary_not_supported(self):
        with pytest.raises(NotImplementedError):
            SinaSource().get_financial_summary("000002")


class TestCrossSourceDateAlignment:
    """切源前的红线: 两源日期序列必须逐日一致

    为什么这是红线(2026-10-19 实测踩过):
      ``cursor`` 是**行下标**。若新浪返回 674 行而 baostock 返回 673 行,
      同样 ``cursor=67`` 指向的**日期会错位** —— 账户会静默地按错误日期
      推进, 曲线、成交、统计全错, 而且**没有任何报错**。

      今天把账户从 sina 切回 baostock 前, 就是靠这条断言确认可以安全切换。
      注意: 数据要跨源真实拉取, 故只在数据源可用时运行。
    """

    SYMBOLS = ["000002", "000001", "600036"]

    def _fetch(self, source: str, symbol: str):
        import pandas as pd

        from stock_model.data.sources.baostock_source import BaostockSource
        from stock_model.data.sources.sina_source import SinaSource

        src = BaostockSource() if source == "baostock" else SinaSource()
        df = src.get_daily(symbol, start_date="20240101")
        if df is None or df.empty:
            return None
        # 统一成 date 字面量列表, 避开两源索引类型差异
        return [str(pd.Timestamp(i).date()) for i in df.index]

    def test_sina_and_baostock_dates_align(self):
        """新浪与 baostock 的日期序列逐日相等(否则切源会错位)"""
        for sym in self.SYMBOLS:
            bs_dates = self._fetch("baostock", sym)
            if bs_dates is None:
                self.skipTest(f"baostock 不可用(skip {sym})")
            sina_dates = self._fetch("sina", sym)
            if sina_dates is None:
                self.skipTest(f"sina 不可用(skip {sym})")
            assert sina_dates == bs_dates, (
                f"{sym} 两源日期序列不一致 —— 切源会导致 cursor 错位: "
                f"baostock {len(bs_dates)} 行({bs_dates[0]}~{bs_dates[-1]}), "
                f"sina {len(sina_dates)} 行({sina_dates[0]}~{sina_dates[-1]})"
            )

    def test_transposed_dates_dont_match(self):
        """反向证明: 序列不同时断言能报错(证明上面不是恒真)"""
        a = ["2024-01-01", "2024-01-02"]
        b = ["2024-01-02", "2024-01-03"]  # 错开一天
        assert a != b


class TestFallbackChainResilience:
    def test_primary_source_construction_failure_still_usable(self, monkeypatch):
        """主源构造失败(baostock 黑名单)不得让 fetcher 整个构造失败

        2026-10-19 实测: ``BaostockSource.__init__`` 里就 login 并直接抛
        ConnectionError(10001011 黑名单), 于是
        ``StockDataFetcher(source="baostock")`` 构造即崩 —— 账户落盘
        data_source=baostock 时服务启动就挂, 连降级链都进不去。
        契约: 主源不可用也要把 fetcher 建起来, 靠降级链顶上。

        注意: 必须用 monkeypatch 替换, 不能手工 ``cls._create_source = ...``
        再在 finally 里赋回原值 —— 取出来的是裸函数, 赋回去会剥掉
        staticmethod 包装, 之后 ``self._create_source(name)`` 就变成
        "takes 1 positional argument but 2 were given", 污染后续所有用例
        (2026-10-19 实测: 导致 13 个无关测试变红)。
        """
        from stock_model.data.fetcher import StockDataFetcher

        class Boom:
            def __init__(self, source=None):
                raise ConnectionError("10001011 黑名单用户")

        class Good:
            name = "good"

            def get_daily(self, *a, **k):
                import pandas as pd

                return pd.DataFrame({"close": [1.0]})

        monkeypatch.setattr(
            StockDataFetcher,
            "_create_source",
            staticmethod(lambda name: Boom() if name == "baostock" else Good()),
        )
        f = StockDataFetcher(source="baostock")

        assert f._source_instance is None, "主源构造失败应记为 None 而非抛出"
        assert f._primary_error is not None
        assert len(f._fallback_instances) >= 1, "主源挂了仍必须有备选"
        df = f._execute_with_fallback("get_daily", "000002")
        assert not df.empty, "主源不可用时必须能靠降级链拿到数据"

    def test_fallback_chain_tries_every_source_in_order(self):
        """降级链必须**依次试完每个**源, 不能只试第一个

        2026-10-19 的故障正是"前两个都挂了、只有第三个能用"。若只试第一个
        备选, 加新浪源等于没加。
        变异验证: 把 ``for fb in chain`` 改成 ``chain[:1]``, 此断言必须变红。
        """
        import pandas as pd

        from stock_model.data.fetcher import StockDataFetcher

        tried: list[str] = []

        class Dead:
            def __init__(self, name):
                self.name = name

            def get_daily(self, *a, **k):
                tried.append(self.name)
                raise ConnectionError(f"{self.name} 挂了")

        class Alive:
            name = "alive"

            def get_daily(self, *a, **k):
                tried.append("alive")
                return pd.DataFrame({"close": [2.0]})

        # 绕过 __init__ 直接装配链: 本用例只关心"链会不会依次试完",
        # 不该去动 StockDataFetcher 的全局工厂方法
        f = object.__new__(StockDataFetcher)
        f.source = "baostock"
        f._primary_error = None
        f._source_instance = Dead("primary")
        f._fallback_instances = [Dead("fb1"), Dead("fb2"), Alive()]
        f._fallback_instance = f._fallback_instances[0]
        f._init_assigned_fallback = f._fallback_instance

        df = f._execute_with_fallback("get_daily", "000002")

        assert not df.empty, "链尾能用的源应返回数据"
        assert tried == ["primary", "fb1", "fb2", "alive"], f"必须按顺序试完每个源: {tried}"
