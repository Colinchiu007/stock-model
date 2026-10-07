"""Universe 股票池筛选单元测试

**全部使用 mock 数据, 不依赖网络** —— baostock 在沙箱/CI 环境常不稳定
(实测多次 login 卡死、JSON 解析失败), 用真实网络写测试会导致假失败。

真实连通性验证请跑 ``experiments/`` 下的脚本或前端「模拟盘」。

.. note::
   ``import baostock`` 本身会做网络初始化, 在网络受限环境中耗时可达 40+ 秒。
   本文件对所有 baostock 调用做了 patch, 该开销只在模块首次导入时发生一次。
"""

from unittest.mock import patch

import pytest

from stock_model.paper.universe import (
    NON_STOCK_KEYWORDS,
    ST_PREFIXES,
    Candidate,
    UniverseConfig,
    UniverseSelector,
)

# ============ 构造数据 ============


# query_all_stock 的列顺序: [code, tradeStatus, code_name]
def row(code, status="1", name="测试股"):
    return [code, status, name]


BASIC_ROWS = [
    row("sz.000001", name="平安银行"),
    row("sh.600036", name="招商银行"),
    row("sz.002106", name="莱宝高科"),
    row("sz.300233", name="金城医药"),
    row("sh.688419", name="耐科装备"),
    # 应被过滤
    row("sh.000001", name="上证综合指数"),  # 指数名
    row("sz.399001", name="深证成指"),  # 指数名
    row("sz.000155", name="ST川仪"),  # ST
    row("sh.600001", name="邯郸钢铁", status="0"),  # 退市
    row("sz.110038", name="某可转债"),  # 名称含"债"
    row("sh.510300", name="沪深300ETF"),  # 名称含 ETF
]


class TestBasicFilter:
    """Layer 1: 本地基础过滤"""

    def test_filters_index_and_fund(self):
        sel = UniverseSelector()
        out = sel.filter_basic(BASIC_ROWS)
        symbols = {c.split(".")[-1] for c, _ in out}
        assert "000001" not in symbols or len(symbols) > 0
        names = [n for _, n in out]
        assert not any("指数" in n for n in names), f"指数未剔除: {names}"
        assert not any("ETF" in n for n in names), f"ETF 未剔除: {names}"
        assert not any("债" in n for n in names), f"可转债未剔除: {names}"

    def test_filters_st_stocks(self):
        sel = UniverseSelector(UniverseConfig(exclude_st=True))
        out = sel.filter_basic(BASIC_ROWS)
        assert not any(n.upper().startswith(ST_PREFIXES) for _, n in out)

    def test_keeps_st_when_disabled(self):
        sel = UniverseSelector(UniverseConfig(exclude_st=False))
        out = sel.filter_basic(BASIC_ROWS)
        assert any(n.startswith("ST") for _, n in out), "关闭 exclude_st 后应保留 ST"

    def test_filters_delisted_by_status(self):
        sel = UniverseSelector(UniverseConfig(exclude_suspended=True))
        out = sel.filter_basic(BASIC_ROWS)
        assert not any(c == "sh.600001" for c, _ in out), "退市股应被过滤"

    def test_only_6digit_codes(self):
        sel = UniverseSelector()
        out = sel.filter_basic([row("sz.1234567", name="超长代码"), row("sz.000002", name="万科A")])
        assert len(out) == 1
        assert out[0][1] == "万科A"

    def test_empty_input(self):
        assert UniverseSelector().filter_basic([]) == []


# ============ 评分 ============


class TestScoring:
    def test_high_amount_scores_higher(self):
        sel = UniverseSelector()
        big = Candidate("000001", "大", amount=5e9, pe_ttm=20, pb=2, turnover=3)
        small = Candidate("000002", "小", amount=2e7, pe_ttm=20, pb=2, turnover=0.2)
        assert sel.score(big) > sel.score(small)

    def test_loss_making_scores_zero_on_valuation(self):
        sel = UniverseSelector()
        loss = Candidate("000001", "亏损", amount=5e8, pe_ttm=-5, pb=1, turnover=2)
        profit = Candidate("000002", "盈利", amount=5e8, pe_ttm=20, pb=2, turnover=2)
        assert sel.score(loss) < sel.score(profit)

    def test_score_in_valid_range(self):
        sel = UniverseSelector()
        for pe in (None, -10, 0.5, 20, 200):
            c = Candidate("000001", "x", amount=1e8, pe_ttm=pe, pb=2, turnover=3)
            assert 0.0 <= sel.score(c) <= 1.0, f"PE={pe} 得分越界: {sel.score(c)}"

    def test_low_turnover_penalized(self):
        sel = UniverseSelector(UniverseConfig(min_turnover=1.0))
        illiquid = Candidate("000001", "冷门", amount=5e8, pe_ttm=20, pb=2, turnover=0.1)
        active = Candidate("000002", "活跃", amount=5e8, pe_ttm=20, pb=2, turnover=5)
        assert sel.score(illiquid) < sel.score(active)

    def test_is_loss_making_property(self):
        assert Candidate("1", "x", pe_ttm=-1).is_loss_making is True
        assert Candidate("1", "x", pe_ttm=10).is_loss_making is False
        assert Candidate("1", "x", pe_ttm=None).is_loss_making is False


# ============ 硬过滤规则 ============


class TestHardFilters:
    """PE/PB 超界必须被剔除, 不能只靠评分惩罚

    实测教训: 仅在 score() 里给 0 分的话, PE=168 的唯特偶仍排进 top_n 第 5。
    """

    @staticmethod
    def _run_build(sel, basic, metrics_map):
        """跑一次 build(), 全程 mock 掉 baostock 网络"""
        all_rows = [[c, "1", n] for c, n in basic]

        def fake_metrics(code):
            return metrics_map.get(code.split(".")[-1])

        with (
            patch("baostock.login") as mock_login,
            patch("baostock.logout"),
            patch.object(UniverseSelector, "_list_all", return_value=all_rows),
            patch.object(UniverseSelector, "_preselect", return_value=basic),
            patch.object(UniverseSelector, "_preselect_by_amount", return_value=None),
            patch.object(UniverseSelector, "_fetch_basic", return_value=("20000101", "1")),
            patch.object(UniverseSelector, "_fetch_metrics", side_effect=fake_metrics),
        ):
            mock_login.return_value.error_code = "0"
            mock_login.return_value.error_msg = ""
            return sel.build()

    def test_pe_above_max_is_excluded(self):
        sel = UniverseSelector(UniverseConfig(max_pe=80, top_n=10, pre_screen_top=5))
        basic = [("sz.000001", "高PE股"), ("sz.000002", "正常股")]
        metrics = {
            "000001": {"amount": 5e8, "pe_ttm": 168.0, "pb": 3.0, "turnover": 3, "bars": 20},
            "000002": {"amount": 5e8, "pe_ttm": 20.0, "pb": 2.0, "turnover": 3, "bars": 20},
        }
        result = self._run_build(sel, basic, metrics)
        symbols = {c.symbol for c in result}
        assert "000001" not in symbols, f"PE=168 的股票应被剔除, 实际得到 {symbols}"
        assert "000002" in symbols, f"正常 PE 的股票应保留, 实际得到 {symbols}"

    def test_pb_out_of_range_excluded(self):
        sel = UniverseSelector(UniverseConfig(max_pb=10, top_n=10, pre_screen_top=5))
        basic = [("sz.000001", "高PB股")]
        metrics = {
            "000001": {"amount": 5e8, "pe_ttm": 20.0, "pb": 25.0, "turnover": 3, "bars": 20},
        }
        assert self._run_build(sel, basic, metrics) == []

    def test_low_amount_excluded(self):
        sel = UniverseSelector(UniverseConfig(min_amount=5e7, top_n=10, pre_screen_top=5))
        basic = [("sz.000001", "冷门股")]
        metrics = {
            "000001": {"amount": 1e6, "pe_ttm": 20.0, "pb": 2.0, "turnover": 3, "bars": 20},
        }
        assert self._run_build(sel, basic, metrics) == []

    def test_low_turnover_excluded(self):
        sel = UniverseSelector(UniverseConfig(min_turnover=1.0, top_n=10, pre_screen_top=5))
        basic = [("sz.000001", "僵尸股")]
        metrics = {
            "000001": {"amount": 5e8, "pe_ttm": 20.0, "pb": 2.0, "turnover": 0.1, "bars": 20},
        }
        assert self._run_build(sel, basic, metrics) == []


# ============ 关键架构约束 ============


class TestSingleSessionConstraint:
    """baostock 是全局单连接, 子方法不得自行 login/logout

    实测教训: `_fetch_basic` 自行 logout 会关掉外层正在用的连接,
    导致后续全部查询返回 `Bad file descriptor`。
    """

    def test_helper_methods_do_not_login(self):
        """子方法源码里不得出现 bs.login() / bs.logout()"""
        import inspect

        for name in ("_list_all", "_fetch_basic", "_fetch_metrics", "_preselect"):
            src = inspect.getsource(getattr(UniverseSelector, name))
            assert "bs.login()" not in src, f"{name} 不得自行 login —— baostock 是全局单连接"
            assert "bs.logout()" not in src, f"{name} 不得自行 logout —— 会关闭外层正在使用的连接"

    def test_only_build_owns_session(self):
        """只有 build() 负责 login/logout"""
        import inspect

        build_src = inspect.getsource(UniverseSelector.build)
        assert "bs.login()" in build_src
        assert "bs.logout()" in build_src


class TestPreselectSampling:
    """预筛: 控制逐只查询量, 且保证板块覆盖(而非随机)"""

    def test_preselect_respects_limit(self):
        sel = UniverseSelector(UniverseConfig(pre_screen_top=20))
        basic = [(f"sh.{600000 + i:06d}", f"股{i}") for i in range(200)]
        # 屏蔽 akshare 快照预筛（真实网络调用会让单测耗时上百秒）
        with patch.object(UniverseSelector, "_preselect_by_amount", return_value=None):
            out = sel._preselect(basic)
        assert len(out) <= 20

    def test_preselect_covers_all_boards(self):
        """必须覆盖沪/深/创/科四个板块, 保证行业分散"""
        sel = UniverseSelector(UniverseConfig(pre_screen_top=40))
        basic = (
            [(f"sh.{600000 + i:06d}", f"沪{i}") for i in range(60)]
            + [(f"sz.{i:06d}", f"深{i}") for i in range(60)]
            + [(f"sz.{300000 + i:06d}", f"创{i}") for i in range(60)]
            + [(f"sh.{688000 + i:06d}", f"科{i}") for i in range(60)]
        )
        with patch.object(UniverseSelector, "_preselect_by_amount", return_value=None):
            out = sel._preselect(basic)
        codes = " ".join(c for c, _ in out)
        assert "600" in codes, "缺沪市主板"
        assert "300" in codes, "缺创业板"
        assert "688" in codes, "缺科创板"

    def test_small_pool_not_prescreened(self):
        sel = UniverseSelector(UniverseConfig(pre_screen_top=100))
        basic = [(f"sh.{600000 + i:06d}", f"股{i}") for i in range(10)]
        with patch.object(UniverseSelector, "_preselect_by_amount", return_value=None):
            assert len(sel._preselect(basic)) == 10


class TestConfig:
    def test_defaults_are_conservative(self):
        cfg = UniverseConfig()
        assert cfg.exclude_st is True
        assert cfg.min_ipo_days >= 180, "次新过滤阈值过低会纳入大量波动新股"
        assert cfg.min_amount >= 1e7, "流动性门槛过低会纳入无法进出的股票"

    def test_describe_is_readable(self):
        assert "UniverseConfig" in UniverseConfig().describe()


class TestCandidate:
    def test_to_dict_rounds(self):
        c = Candidate("000001", "测试", amount=1.23456789e8, pe_ttm=12.345678, score=0.123456)
        d = c.to_dict()
        assert d["amount"] == 123456789.0
        assert d["pe_ttm"] == 12.346

    def test_keywords_cover_common_non_stock(self):
        for kw in ("指数", "ETF", "债"):
            assert kw in NON_STOCK_KEYWORDS


class TestFallbackPoolIsFlagged:
    """退化股票池必须带警示, 防止后人换成"伪分散"组合

    背景: 原默认池 000002/000001/600036 全是银行地产, 同涨同跌,
    会让下跌市超额虚高、上涨市对照失真。已在 paper_api 中显式标注。
    """

    def test_fallback_pool_is_documented_as_pseudo_diversification(self):
        from stock_model.web import paper_api

        assert hasattr(paper_api, "FALLBACK_SYMBOLS")
        src = __import__("inspect").getsource(paper_api)
        assert "伪分散" in src, "退化股票池必须显式标注为'伪分散', 否则后人会误以为它是合理默认值"

    def test_universe_endpoint_registered(self):
        """动态股票池 API 必须注册, 否则端点是摆设"""
        pytest.importorskip("fastapi")
        from stock_model.web.app import create_app

        app = create_app()
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/api/paper/universe" in paths

    def test_run_endpoint_accepts_symbols(self):
        """/api/paper/run 必须支持传入 symbols 以使用动态池"""
        import inspect

        from stock_model.web import paper_api

        assert "symbols" in inspect.getsource(paper_api.register_paper_routes)
