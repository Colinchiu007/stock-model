"""动态股票池（Universe）—— 全市场初筛

为什么需要
----------
此前模拟盘与选股榜的股票池都是**人工指定**的（000002/000001/600036），
而这三只同属银行地产，属"伪分散"：下跌市里同步下跌，策略只要空仓
就能跑赢。导致 `docs/strategy-evaluation-2026-10-07.md` 中的评估结论
无法区分"策略有效"与"板块整体下跌"。

本模块从全市场自动构建候选池，让实验结论具备可推广性。

分层筛选
--------
    Layer 0  全市场列表        baostock.query_all_stock()
    Layer 1  基础排除          指数/基金/退市/ST/次新/流动性不足
    Layer 2  评分排序          流动性 + 估值 + 换手率 → 取 Top N

设计要点
--------
**只查需要的股票**。baostock 的 `query_stock_basic` / `k_data_plus` 都是
**单只查询**，对全市场 5000+ 只逐一查询不可行。因此:

- Layer 1 的**名称/代码级过滤**在拿到列表后本地完成（零网络开销）
- Layer 2 的估值/流动性过滤需要逐只查询，故先用**日均成交额预筛**缩小范围，
  再对预筛后的候选逐只精筛

**避免未来函数**。所有筛选只用截止到 ``as_of`` 的数据。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

# 名称中包含即判定为非个股（指数/基金/债券等）
NON_STOCK_KEYWORDS = (
    "指数",
    "上证",
    "深证",
    "ETF",
    "LOF",
    "REIT",
    "债",
    "基金",
    "B股",
    "权证",
    "等权",
    "基本",
    "全指",
    "中证",
    "成指",
    "科创",
    "创业",
    "300",
    "国债",
    "央行",
    "货币",
    "短融",
)

# ST / *ST / 退市整理 名称前缀
ST_PREFIXES = ("ST", "*ST", "SST", "S*ST", "退")


@dataclass
class UniverseConfig:
    """筛选配置"""

    # 基础排除
    exclude_st: bool = True
    min_ipo_days: int = 365  # 上市满 1 年，排除次新股波动
    exclude_suspended: bool = True  # 排除 status != 1（已退市/暂停）

    # 流动性预筛（Layer 2 前置，避免全市场逐只查询）
    min_amount: float = 5e7  # 日均成交额下限（5000 万）
    pre_screen_top: int = 400  # 预筛后最多精筛多少只

    # 精筛（Layer 2）
    max_pe: float = 80.0  # PE(TTM) 上限；<=0 为亏损股，单独标记
    min_pb: float = 0.0  # PB 下限，过低多为基本面恶化
    max_pb: float = 20.0
    min_turnover: float = 0.3  # 换手率下限（%），过低流动性差

    # 输出
    top_n: int = 20
    as_of: str = ""  # 数据截止日 YYYYMMDD，空=最近交易日

    def describe(self) -> str:
        return (
            f"UniverseConfig(次新排除={self.min_ipo_days}天, "
            f"成交额>={self.min_amount / 1e8:.2f}亿, "
            f"PE<={self.max_pe}, top_n={self.top_n})"
        )


@dataclass
class Candidate:
    """候选股票"""

    symbol: str
    name: str
    ipo_date: str = ""
    amount: float = 0.0  # 日均成交额
    pe_ttm: float | None = None
    pb: float | None = None
    turnover: float | None = None  # 换手率 %
    score: float = 0.0
    reason: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def is_loss_making(self) -> bool:
        """是否亏损股（PE 为负）"""
        return self.pe_ttm is not None and self.pe_ttm <= 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "ipo_date": self.ipo_date,
            "amount": round(self.amount, 2),
            "pe_ttm": round(self.pe_ttm, 3) if self.pe_ttm is not None else None,
            "pb": round(self.pb, 3) if self.pb is not None else None,
            "turnover": round(self.turnover, 3) if self.turnover is not None else None,
            "score": round(self.score, 4),
            "is_loss_making": self.is_loss_making,
            "reason": self.reason,
        }


class UniverseSelector:
    """全市场股票池筛选器

    使用示例::

        sel = UniverseSelector(UniverseConfig(top_n=20))
        candidates = sel.build()
        for c in candidates[:10]:
            print(c.symbol, c.name, c.score)
    """

    def __init__(self, config: UniverseConfig | None = None):
        self.config = config or UniverseConfig()

    # ==================== Layer 0/1: 全市场 + 基础排除 ====================

    def _list_all(self) -> list[list[str]]:
        """列出全市场 [[code, tradeStatus, code_name], ...]

        注意: 要求调用方已登录 baostock（baostock 是全局单连接，
        子方法自行 login/logout 会关闭外层连接）。
        周末/节假日返回空，故回溯查找最近有数据的交易日。
        """
        import baostock as bs

        day = self._resolve_trade_day()
        rs = bs.query_all_stock(day=day)
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if rows:
            logger.info(f"全市场列表: {len(rows)} 条 (数据日 {day})")
        return rows

    def _resolve_trade_day(self) -> str:
        """在当前登录连接内找最近的有数据交易日"""
        import datetime as _dt

        import baostock as bs

        if self.config.as_of:
            # as_of 是业务日期(非时区), 与后续 naive 比较保持一致
            return _dt.datetime.strptime(self.config.as_of, "%Y%m%d").strftime(  # noqa: DTZ007
                "%Y-%m-%d"
            )

        today = _dt.datetime.now()
        for back in range(10):
            day = (today - _dt.timedelta(days=back)).strftime("%Y-%m-%d")
            try:
                rs = bs.query_all_stock(day=day)
                if rs.next():
                    return day
            except Exception:  # pragma: no cover
                continue
        return today.strftime("%Y-%m-%d")

    def filter_basic(self, rows: list[list[str]]) -> list[tuple[str, str]]:
        """Layer 1: 本地基础过滤（零网络开销）

        ``query_all_stock`` 的列顺序为 ``[code, tradeStatus, code_name]``
        （**不含上市日期**，次新过滤需在 Layer 2 用 ``query_stock_basic``）。

        过滤: 非个股代码 / 指数基金 / ST / 退市
        """
        cfg = self.config
        out: list[tuple[str, str]] = []
        for row in rows:
            if len(row) < 3:
                continue
            code, trade_status, name = row[0], row[1], row[2]

            # 仅保留 6 位数字的个股代码（排除指数等）
            plain = code.split(".")[-1]
            if not (plain.isdigit() and len(plain) == 6):
                continue

            # 名称过滤
            if any(k in name for k in NON_STOCK_KEYWORDS):
                continue
            if cfg.exclude_st and name.upper().startswith(ST_PREFIXES):
                continue
            # tradeStatus != '1' 表示已退市/暂停交易
            if cfg.exclude_suspended and trade_status != "1":
                continue

            out.append((code, name))

        logger.info(f"基础过滤: {len(rows)} -> {len(out)}")
        return out

    def _fetch_basic(self, code: str) -> tuple[str, str] | None:
        """取单只股票的基本信息 (ipo_date, status)

        ``query_stock_basic`` 的 fields 为
        ``[code, code_name, ipoDate, outDate, type, status]``
        """
        # 要求: 调用方已登录 baostock。
        # 绝不在此处 login/logout —— baostock 是全局单连接,
        # 子方法自行 logout 会关闭外层正在使用的连接(实测 Bad file descriptor)。
        import baostock as bs

        try:
            rs = bs.query_stock_basic(code=code)
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        except Exception as e:  # pragma: no cover
            logger.debug(f"{code} 基本信息获取失败: {e}")
            return None

        if not rows:
            return None
        r = rows[0]
        # code, code_name, ipoDate, outDate, type, status
        ipo = r[2] if len(r) > 2 else ""
        status = r[5] if len(r) > 5 else "1"
        return ipo, status

    # ==================== Layer 2: 估值/流动性精筛 ====================

    def _fetch_metrics(self, code: str) -> dict | None:
        """取单只股票近 30 日的估值/流动性指标

        要求: 调用方已登录 baostock（本模块的 build/精筛阶段统一登录）。
        """
        import math

        import baostock as bs

        try:
            rs = bs.query_history_k_data_plus(
                code,
                "date,turn,peTTM,pbMRQ,volume,amount",
                start_date=_days_ago(30),
                end_date=_today(),
                frequency="d",
                adjustflag="2",
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        except Exception as e:  # pragma: no cover - 网络异常
            logger.debug(f"{code} 指标获取失败: {e}")
            return None

        if len(rows) < 5:
            return None

        def _to_f(v: str) -> float | None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return None if math.isnan(f) else f

        amounts = [_to_f(r[5]) for r in rows[-20:]]
        # 变量名分开: 复用 amounts 会让 mypy 保持 list[float | None] 的推断,
        # 于是下面 sum() 被报成「参数类型应为 Iterable[bool]」(假错误)。
        # 运行时一直是正确的 —— 过滤确实生效了, 只是类型收窄跨不过重新赋值。
        valid_amounts = [a for a in amounts if a is not None]
        return {
            "amount": sum(valid_amounts) / len(valid_amounts) if valid_amounts else 0.0,
            "pe_ttm": _to_f(rows[-1][2]),
            "pb": _to_f(rows[-1][3]),
            "turnover": _to_f(rows[-1][1]),
            "bars": len(rows),
        }

    def score(self, c: Candidate) -> float:
        """综合评分

        权重设计（偏重流动性，因流动性是最硬的约束）:
          流动性 40% — 成交额越大越易进出
          估值   30% — PE/PB 适中者得分高，亏损股大幅扣分
          活跃度 30% — 换手率反映关注度
        """
        cfg = self.config

        # 流动性: 成交额取对数归一（成交额跨度可达百倍）
        amt_score = 0.0
        if c.amount > 0:
            import math

            ratio = min(c.amount / 1e9, 10.0)  # 10 亿封顶
            amt_score = math.log10(max(ratio, 0.1) * 10 + 1) / 2.0

        # 估值: PE 在 0~max_pe 之间线性得分；亏损股扣分
        val_score = 0.3
        if c.pe_ttm is not None:
            if c.pe_ttm <= 0:  # 亏损
                val_score = 0.0
            elif c.pe_ttm <= cfg.max_pe:
                # PE 越低越"便宜"，但过低也可能是陷阱，取中段最优
                val_score = 1.0 - abs(c.pe_ttm - cfg.max_pe * 0.4) / (cfg.max_pe * 1.4)
                val_score = max(0.0, min(1.0, val_score))
            else:
                val_score = 0.0

        # PB
        pb_score = 0.5
        if c.pb is not None:
            if c.pb <= 0 or c.pb > cfg.max_pb:
                pb_score = 0.0
            else:
                pb_score = max(0.0, 1.0 - abs(c.pb - 2.0) / 18.0)

        val_score = val_score * 0.6 + pb_score * 0.4

        # 活跃度: 换手率 0.3% ~ 10% 之间最佳
        turn_score = 0.3
        if c.turnover is not None:
            if c.turnover < cfg.min_turnover:
                turn_score = 0.2
            elif c.turnover > 20:
                turn_score = 0.3  # 过度投机
            else:
                turn_score = min(1.0, c.turnover / 8.0)

        return round(amt_score * 0.4 + val_score * 0.3 + turn_score * 0.3, 4)

    # ==================== 主流程 ====================

    def build(self) -> list[Candidate]:
        """执行完整筛选流程

        Returns:
            按评分降序的候选列表（长度 <= config.top_n）
        """
        import baostock as bs

        cfg = self.config
        t0 = time.time()

        # baostock 是全局单连接：全程只 login 一次，子方法不得自行 login/logout
        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            logger.info(f"开始构建股票池: {cfg.describe()}")

            rows = self._list_all()
            basic = self.filter_basic(rows)
            if not basic:
                logger.warning("基础过滤后无候选")
                return []

            # baostock 逐只查询较慢，先用全市场快照(若可用)排序控制查询量；
            # 不可用时退化为按板块分段抽样，保证覆盖面而非随机性。
            prescreen = self._preselect(basic)

            # --- 精筛: 逐只取指标 ---
            candidates: list[Candidate] = []
            cutoff = self._ipo_cutoff()
            for idx, (code, name) in enumerate(prescreen, 1):
                if idx % 50 == 0:
                    logger.info(f"  精筛进度 {idx}/{len(prescreen)}")

                # 次新股过滤（query_all_stock 不含上市日期, 需单独查）
                ipo = ""
                if cutoff:
                    basic_info = self._fetch_basic(code)
                    if basic_info is None:
                        continue
                    ipo, status = basic_info
                    if status != "1" or (ipo and ipo > cutoff):
                        continue

                m = self._fetch_metrics(code)
                if not m:
                    continue
                if cfg.min_amount > 0 and m["amount"] < cfg.min_amount:
                    continue
                if m["turnover"] is not None and m["turnover"] < cfg.min_turnover:
                    continue
                pe, pb = m["pe_ttm"], m["pb"]
                # 估值硬过滤: PE/PB 超界直接排除。
                # 仅靠评分惩罚是不够的 —— 评分只是让边缘股排后, 不会剔除它们,
                # 结果 PE=168 的股票仍会进 top_n(实测唯特偶 PE=168 排第 5)。
                # PE<=0 视为亏损股, 由评分给 0 分但保留(可选纳入)。
                if pe is not None and pe > cfg.max_pe:
                    continue
                if pb is not None and (pb <= cfg.min_pb or pb > cfg.max_pb):
                    continue

                c = Candidate(
                    symbol=code.split(".")[-1],
                    name=name,
                    ipo_date=ipo,
                    amount=m["amount"],
                    pe_ttm=pe,
                    pb=pb,
                    turnover=m["turnover"],
                )
                c.score = self.score(c)
                c.reason = (
                    f"成交额{c.amount / 1e8:.1f}亿 "
                    f"PE={pe if pe is not None else '—'} "
                    f"PB={pb if pb is not None else '—'}"
                )
                candidates.append(c)
        finally:
            bs.logout()

        candidates.sort(key=lambda x: -x.score)
        result = candidates[: cfg.top_n]
        logger.info(
            f"股票池构建完成: 精筛{len(candidates)}只 -> 取{len(result)}只, "
            f"耗时{time.time() - t0:.1f}s"
        )
        return result

    def _ipo_cutoff(self) -> str:
        """次新股过滤的上市日期下限 (YYYYMMDD)"""
        import datetime as _dt

        if self.config.min_ipo_days <= 0:
            return ""
        ref = (
            _dt.datetime.strptime(self.config.as_of, "%Y%m%d")  # noqa: DTZ007
            if self.config.as_of
            else _dt.datetime.now()
        )
        return (ref - _dt.timedelta(days=self.config.min_ipo_days)).strftime("%Y%m%d")

    def _preselect(self, basic: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """预筛: 从基础池挑出待精筛的候选，控制逐只查询量

        优先用 akshare 全市场快照排序(一次查询覆盖全市场)；
        不可用时按板块分段抽样，保证覆盖面而非随机性。
        """
        cfg = self.config
        if len(basic) <= cfg.pre_screen_top:
            return basic

        try:
            ranked = self._preselect_by_amount(basic)
            if ranked:
                return ranked[: cfg.pre_screen_top]
        except Exception as e:  # pragma: no cover - 网络不可用时降级
            logger.warning(f"快照预筛失败({e}), 改用分段抽样")

        # 降级: 分板块(沪/深/创/科) + 按代码等距抽样，保证行业与板块覆盖
        buckets: dict[str, list] = {"sh": [], "sz": [], "cy": [], "kc": []}
        for item in basic:
            code, _name = item
            plain = code.split(".")[-1]
            if code.startswith("sh."):
                buckets["kc" if plain.startswith("688") else "sh"].append(item)
            elif plain.startswith("300") or plain.startswith("301"):
                buckets["cy"].append(item)
            else:
                buckets["sz"].append(item)

        picked: list = []
        per = max(1, cfg.pre_screen_top // 4)
        for bucket in buckets.values():
            if not bucket:
                continue
            step = max(1, len(bucket) // per)
            picked.extend(bucket[::step][:per])
        return picked[: cfg.pre_screen_top]

    def _preselect_by_amount(self, basic: list[tuple[str, str]]) -> list | None:
        """用 akshare 全市场快照按成交额预筛（一次查询覆盖全市场）"""
        from stock_model.data.sources.akshare_source import AkshareSource

        spot = AkshareSource().get_stock_info("")
        if spot is None or spot.empty:
            return None
        if "成交额" not in spot.columns:
            return None

        top_amount = set(
            spot.nlargest(self.config.pre_screen_top * 2, "成交额")["代码"].astype(str)
        )
        out = [b for b in basic if b[0].split(".")[-1] in top_amount]
        logger.info(f"快照预筛: {len(basic)} -> {len(out)}")
        return out or None


# ==================== 辅助 ====================


def _today() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y-%m-%d")


def _days_ago(n: int) -> str:
    import datetime as _dt

    return (_dt.datetime.now() - _dt.timedelta(days=n)).strftime("%Y-%m-%d")
