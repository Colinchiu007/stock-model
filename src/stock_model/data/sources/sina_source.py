"""新浪财经数据源(免费、无需注册)

为什么加它(2026-10-19 实测触发)
--------------------------------
当天 baostock 返回 ``10001011 黑名单用户`` / ``10002007``,
akshare 底层的东方财富被 ``RemoteDisconnected`` —— **两个既有源同时不可用**,
模拟盘无法推进, 选股与回测全瘫。

本机实测新浪 ``quotes.sina.cn`` 直连可用(约 0.1s, 字段齐全), 且不依赖
前两者的上游, 故作为**第三道冗余**接入。

接口特性(实测结论, 直接决定下面的实现方式)
------------------------------------------
1. 只能取"最近 N 根"(``datalen``), **没有 start_date/end_date 参数** ——
   所以取回最近 N 根后必须在本地按区间切片。
2. ``datalen`` 上限约 1000: 请求 2000 时返回的已不是 JS 字面量格式,
   ``ast.literal_eval`` 直接 SyntaxError。故硬封顶 1000。
3. 市场前缀: ``sz``(深市/创业板) / ``sh``(沪市/科创板), 实测四种前缀均可用。
4. 返回字段: day/open/high/low/close/volume + 若干均线(忽略)。

能力边界
--------
只提供行情。**估值、财务摘要、板块、实时报价都没有对应接口** ——
这些方法显式抛 ``NotImplementedError`` 而不是返回空表, 让降级链路
能正确跳过它去试下一个源(返回空表会被误判为"成功但无数据")。
"""

from __future__ import annotations

import ast
import urllib.request
from datetime import datetime
from typing import Any

import pandas as pd
from loguru import logger

from stock_model.data.sources.base import DataSource

KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketDataService.getKLineData?symbol={symbol}&scale={scale}&datalen={datalen}"
)

# 实测: datalen>1000 返回非 JS 字面量内容, 无法解析
DATALEN_API_MAX = 1000
# 取回后本地切片前预留的余量(区间两端各留一些交易日, 避免边界差一根)
DATALEN_MARGIN = 30

DEFAULT_TIMEOUT = 20
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
}

# scale: 分钟数 -> 周期(日 240 / 周 1200 / 月 7200)
_SCALE_DAILY, _SCALE_WEEKLY, _SCALE_MONTHLY = 240, 1200, 7200


class SinaSource(DataSource):
    """新浪财经行情源(只读日/周/月线)"""

    name = "sina"

    # ==================== 内部工具 ====================

    @staticmethod
    def _convert_symbol(symbol: str) -> str:
        """6 位代码 -> 新浪格式(``sz``/``sh`` 前缀)

        规则(与沪深市场惯例一致):
          - 6/9 开头(深市含创业板 300/301) -> sz
          - 其余 0/3 开头已含, 5/6/68 开头(沪市/科创板) -> sh
        """
        if symbol.startswith(("sz", "sh")):
            return symbol
        if symbol.startswith(("00", "30", "20")):
            return f"sz{symbol}"
        return f"sh{symbol}"

    @staticmethod
    def _datalen_for(start_date: str | None, end_date: str | None) -> int:
        """按请求区间估算需要多少根 K 线

        新浪没有区间参数, 只能"取最近 N 根再本地切片"。

        估算取**自然日跨度**(每日最多 1 根 K 线, 即 1:1 上界)而不是
        按 5/7 折算交易日 —— 折算会在区间起点恰好落在长假之前时少取,
        导致切片后缺历史(实测 2024 全年折算得 290, 而接口实际需要覆盖
        到区间首日之前的所有交易日)。宁可多取(上限 1000 内)也不能少取:
        少取会静默丢数据, 多取只是几次本地切片。
        """
        if not start_date:
            return DATALEN_API_MAX
        try:
            # 这里的日期只用于**算天数差**, 不与任何带时区的时间戳比较,
            # 所以保持 naive 是正确的(接口本身就是"最近 N 根"的相对语义)。
            start = datetime.strptime(str(start_date)[:8], "%Y%m%d")  # noqa: DTZ007
        except ValueError:
            return DATALEN_API_MAX
        end = (
            datetime.strptime(str(end_date)[:8], "%Y%m%d")  # noqa: DTZ007
            if end_date
            else datetime.now()
        )
        if end <= start:
            return DATALEN_API_MAX
        days = (end - start).days
        # 自然日 + 余量; 封顶接口上限
        est = days + DATALEN_MARGIN
        return max(60, min(DATALEN_API_MAX, est))

    def _http_get(self, url: str) -> str:
        """GET 并返回文本(带 UA/Referer, 部分接口对缺失头敏感)"""
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            text: str = resp.read().decode("utf-8", errors="ignore")
        return text

    def _fetch_rows(
        self, sina_symbol: str, datalen: int, scale: int = _SCALE_DAILY
    ) -> list[dict[str, Any]]:
        """抓取并解析 K 线行

        新浪返回的是 **JS 对象字面量**(key 无引号), 所以用
        ``ast.literal_eval`` 而非 ``json.loads``。
        """
        url = KLINE_URL.format(symbol=sina_symbol, scale=scale, datalen=datalen)
        text = self._http_get(url)
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            rows = ast.literal_eval(text[start : end + 1])
        except (ValueError, SyntaxError):
            # datalen 过大时接口会返回非字面量内容(实测 2000 条即如此)
            logger.warning(
                f"[sina] {sina_symbol} datalen={datalen} 响应无法解析"
                f"(接口上限约 {DATALEN_API_MAX}), 视为无数据"
            )
            return []
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _rows_to_df(rows: list[dict[str, Any]], symbol: str) -> pd.DataFrame:
        """原始行 -> 标准 DataFrame(与其他源对齐)"""
        cols = ["open", "high", "low", "close", "volume"]
        if not rows:
            return pd.DataFrame(columns=[*cols, "symbol"])
        df = pd.DataFrame(rows)
        keep = [c for c in ("day", *cols) if c in df.columns]
        df = df[keep].copy()
        df = df.rename(columns={"day": "date"})
        for col in cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).set_index("date").sort_index()
        df["symbol"] = symbol
        return df

    @staticmethod
    def _slice_window(
        df: pd.DataFrame, start_date: str | None, end_date: str | None
    ) -> pd.DataFrame:
        """按请求区间本地切片(新浪没有区间参数, 只能取回后切)"""
        if df.empty:
            return df
        if start_date:
            start = pd.to_datetime(str(start_date)[:8], format="%Y%m%d", errors="coerce")
            if pd.notna(start):
                df = df[df.index >= start]
        if end_date:
            end = pd.to_datetime(str(end_date)[:8], format="%Y%m%d", errors="coerce")
            if pd.notna(end):
                df = df[df.index <= end]
        return df

    @staticmethod
    def _check_adjust(adjust: str) -> None:
        """新浪接口固定是前复权口径, 不支持后复权

        与其返回错口径的数据, 不如显式失败让降级链路去试别的源。
        """
        if adjust not in ("qfq", ""):
            raise ValueError(f"新浪源不支持复权类型 {adjust!r}(仅支持 qfq/空=不复权口径)")

    def _kline(
        self,
        symbol: str,
        start_date: str | None,
        end_date: str | None,
        adjust: str,
        scale: int,
    ) -> pd.DataFrame:
        """日/周/月线的共同实现"""
        self._check_adjust(adjust)
        sina_sym = self._convert_symbol(symbol)
        datalen = self._datalen_for(start_date, end_date)
        rows = self._fetch_rows(sina_sym, datalen, scale)
        if not rows:
            logger.warning(f"[sina] {sina_sym} 无数据(scale={scale} datalen={datalen})")
            return pd.DataFrame()
        df = self._rows_to_df(rows, symbol)
        return self._slice_window(df, start_date, end_date)

    # ==================== DataSource 契约 ====================

    def get_daily(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """日线行情"""
        return self._kline(symbol, start_date, end_date, adjust, _SCALE_DAILY)

    def get_weekly(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """周线行情"""
        return self._kline(symbol, start_date, end_date, adjust, _SCALE_WEEKLY)

    def get_monthly(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """月线行情"""
        return self._kline(symbol, start_date, end_date, adjust, _SCALE_MONTHLY)

    # ---------- 无对应接口的能力: 显式失败而非返回空表 ----------

    def get_realtime(self, symbol: str) -> pd.DataFrame:
        raise NotImplementedError("新浪源未接入实时报价接口(选股/回测不依赖该能力)")

    def get_stock_info(self, symbol: str) -> pd.DataFrame:
        raise NotImplementedError("新浪源未接入股票基本信息接口")

    def get_valuation(self, symbol: str) -> pd.DataFrame:
        raise NotImplementedError("新浪源未接入估值(PE/PB)接口")

    def get_financial_summary(self, symbol: str) -> pd.DataFrame:
        raise NotImplementedError("新浪源未接入财务摘要接口")

    def get_sector_list(self) -> pd.DataFrame:
        raise NotImplementedError("新浪源未接入行业板块接口")

    def get_sector_stocks(self, sector: str) -> pd.DataFrame:
        raise NotImplementedError("新浪源未接入板块成分股接口")
