"""Baostock 数据源实现

基于证券宝(baostock.com)的免费A股数据接口。
相比akshare(东方财富)，baostock更稳定，不受反爬策略影响。

特点:
  - 独立的数据服务器，不依赖东方财富
  - 不会因IP限流或反爬策略导致连接失败
  - 支持日线/周线/月线K线、复权数据、行业分类等
  - 需要login/logout管理连接

限制:
  - 实时行情数据不如akshare丰富
  - 股票代码格式为 "sh.600000" / "sz.000001"
  - 部分数据字段精度较高(多位小数)
"""

from __future__ import annotations

import threading
from typing import Optional

import baostock as bs
import pandas as pd
from loguru import logger

from stock_model.data.sources.base import DataSource


class BaostockSource(DataSource):
    """Baostock 数据源

    基于证券宝的免费数据接口，稳定可靠，不受反爬策略影响。
    """

    name = "baostock"

    # 线程锁，确保login/logout线程安全
    _lock = threading.Lock()
    _logged_in = False

    def __init__(self) -> None:
        self._ensure_login()

    def _ensure_login(self) -> None:
        """确保已登录baostock服务"""
        with self._lock:
            if not BaostockSource._logged_in:
                lg = bs.login()
                if lg.error_code != "0":
                    raise ConnectionError(
                        f"baostock登录失败: {lg.error_code} {lg.error_msg}"
                    )
                BaostockSource._logged_in = True
                logger.info("baostock登录成功")

    @staticmethod
    def logout() -> None:
        """登出baostock服务"""
        with BaostockSource._lock:
            if BaostockSource._logged_in:
                bs.logout()
                BaostockSource._logged_in = False
                logger.info("baostock已登出")

    @staticmethod
    def _convert_symbol(symbol: str) -> str:
        """将标准6位股票代码转换为baostock格式

        "000001" -> "sz.000001"
        "600000" -> "sh.600000"
        "300001" -> "sz.300001"  (创业板)
        "688001" -> "sh.688001"  (科创板)
        "830001" -> "bj.830001"  (北交所) -- baostock可能不支持
        "sh.600000" -> "sh.600000" (已转换的直接返回)
        """
        # 已包含市场前缀的直接返回
        if "." in symbol:
            return symbol

        if symbol.startswith(("6", "9")):
            return f"sh.{symbol}"
        elif symbol.startswith(("0", "1", "2", "3")):
            return f"sz.{symbol}"
        elif symbol.startswith("8"):
            # 北交所，baostock可能不支持
            logger.warning(f"北交所股票 {symbol} 可能不被baostock支持")
            return f"bj.{symbol}"
        else:
            logger.warning(f"未知股票代码格式: {symbol}")
            return f"sz.{symbol}"

    @staticmethod
    def _normalize_date(date_str: Optional[str]) -> Optional[str]:
        """将日期格式标准化为baostock要求的 YYYY-MM-DD 格式

        "20240101" -> "2024-01-01"
        "2024-01-01" -> "2024-01-01"
        None -> None
        """
        if date_str is None:
            return None
        date_str = date_str.strip()
        if len(date_str) == 8 and date_str.isdigit():
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        return date_str

    @staticmethod
    def _convert_adjust(adjust: str) -> str:
        """将标准复权标识转换为baostock格式

        "qfq" -> "2" (前复权)
        "hfq" -> "1" (后复权)
        "" -> "3" (不复权)
        """
        mapping = {"qfq": "2", "hfq": "1", "": "3"}
        return mapping.get(adjust, "2")

    def get_daily(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        bs_symbol = self._convert_symbol(symbol)
        adjust_flag = self._convert_adjust(adjust)
        start = self._normalize_date(start_date) or "1990-01-01"
        end = self._normalize_date(end_date) or "2099-12-31"
        logger.debug(f"[baostock] 获取日线: {bs_symbol}, {start} ~ {end}")

        rs = bs.query_history_k_data_plus(
            bs_symbol,
            "date,open,high,low,close,volume,amount,turn,pctChg",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag=adjust_flag,
        )

        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock查询失败: {rs.error_code} {rs.error_msg}"
            )

        data = []
        while rs.next():
            data.append(rs.get_row_data())

        if not data:
            logger.warning(f"[baostock] {bs_symbol} 无数据返回")
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)
        return self._normalize(df, symbol)

    def get_weekly(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        bs_symbol = self._convert_symbol(symbol)
        adjust_flag = self._convert_adjust(adjust)
        start = self._normalize_date(start_date) or "1990-01-01"
        end = self._normalize_date(end_date) or "2099-12-31"
        logger.debug(f"[baostock] 获取周线: {bs_symbol}, {start} ~ {end}")

        rs = bs.query_history_k_data_plus(
            bs_symbol,
            "date,open,high,low,close,volume,amount,turn,pctChg",
            start_date=start,
            end_date=end,
            frequency="w",
            adjustflag=adjust_flag,
        )

        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock查询失败: {rs.error_code} {rs.error_msg}"
            )

        data = []
        while rs.next():
            data.append(rs.get_row_data())

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)
        return self._normalize(df, symbol)

    def get_monthly(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        bs_symbol = self._convert_symbol(symbol)
        adjust_flag = self._convert_adjust(adjust)
        start = self._normalize_date(start_date) or "1990-01-01"
        end = self._normalize_date(end_date) or "2099-12-31"
        logger.debug(f"[baostock] 获取月线: {bs_symbol}, {start} ~ {end}")

        rs = bs.query_history_k_data_plus(
            bs_symbol,
            "date,open,high,low,close,volume,amount,turn,pctChg",
            start_date=start,
            end_date=end,
            frequency="m",
            adjustflag=adjust_flag,
        )

        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock查询失败: {rs.error_code} {rs.error_msg}"
            )

        data = []
        while rs.next():
            data.append(rs.get_row_data())

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)
        return self._normalize(df, symbol)

    def get_realtime(self, symbol: str) -> pd.DataFrame:
        """baostock不直接支持实时行情，返回最近交易日数据"""
        logger.warning("[baostock] 不支持实时行情，返回最近日线数据")
        return self.get_daily(symbol)

    def get_stock_info(self, symbol: str) -> pd.DataFrame:
        """获取股票基本信息"""
        bs_symbol = self._convert_symbol(symbol)
        logger.debug(f"[baostock] 获取股票信息: {bs_symbol}")

        rs = bs.query_stock_industry()
        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock查询行业失败: {rs.error_code} {rs.error_msg}"
            )

        data = []
        while rs.next():
            row = rs.get_row_data()
            # row格式: [updateDate, code, code_name, industry, industryClassification]
            if row[1] == bs_symbol:
                data.append(row)

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)
        return df

    def get_sector_list(self) -> pd.DataFrame:
        """获取行业板块列表"""
        logger.debug("[baostock] 获取行业板块列表")

        rs = bs.query_stock_industry()
        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock查询行业失败: {rs.error_code} {rs.error_msg}"
            )

        data = []
        while rs.next():
            data.append(rs.get_row_data())

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)
        # 去重获取行业列表
        if "industry" in df.columns:
            industries = df["industry"].unique()
            return pd.DataFrame({"industry": industries})
        return df

    def get_sector_stocks(self, sector: str) -> pd.DataFrame:
        """获取板块成分股"""
        logger.debug(f"[baostock] 获取板块成分股: {sector}")

        rs = bs.query_stock_industry()
        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock查询行业失败: {rs.error_code} {rs.error_msg}"
            )

        data = []
        while rs.next():
            row = rs.get_row_data()
            if sector in str(row):
                data.append(row)

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)
        return df

    def get_valuation(self, symbol: str) -> pd.DataFrame:
        """获取估值数据(PE/PB/PS等)

        使用 baostock 的 query_history_k_data_plus 获取含估值指标的数据。
        baostock K线数据支持 peTTM/pbMRQ/psTTM 等字段。
        """
        bs_symbol = self._convert_symbol(symbol)
        logger.debug(f"[baostock] 获取估值数据: {bs_symbol}")

        # baostock的K线数据支持估值字段: peTTM, pbMRQ, psTTM, pcfNcfTTM
        fields = "date,peTTM,pbMRQ,psTTM,pcfNcfTTM"
        rs = bs.query_history_k_data_plus(
            code=bs_symbol,
            fields=fields,
            start_date="2020-01-01",
            end_date="2099-12-31",
            frequency="d",
            adjustflag="3",
        )
        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock查询估值数据失败: {rs.error_code} {rs.error_msg}"
            )

        data = []
        while rs.next():
            data.append(rs.get_row_data())

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)

        # 标准化列名
        column_map = {
            "peTTM": "pe_ttm",
            "pbMRQ": "pb",
            "psTTM": "ps_ttm",
            "pcfNcfTTM": "pcf_ttm",
        }
        df = df.rename(columns=column_map)

        # 转换数值类型
        numeric_cols = ["pe_ttm", "pb", "ps_ttm", "pcf_ttm"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 设置日期索引
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")

        df["symbol"] = symbol
        return df

    def get_financial_summary(self, symbol: str) -> pd.DataFrame:
        """获取财务摘要数据

        使用 baostock 的 query_profit_data 获取盈利数据，
        query_operation_data 获取运营数据。
        """
        bs_symbol = self._convert_symbol(symbol)
        logger.debug(f"[baostock] 获取财务摘要: {bs_symbol}")

        # 获取盈利数据(roeAvg, npMargin, gpMargin, netProfit, epsTTM等)
        rs = bs.query_profit_data(code=bs_symbol, year=2024, quarter=4)
        if rs.error_code != "0":
            raise RuntimeError(
                f"baostock查询盈利数据失败: {rs.error_code} {rs.error_msg}"
            )

        data = []
        while rs.next():
            data.append(rs.get_row_data())

        if not data:
            # 尝试上一年数据
            rs = bs.query_profit_data(code=bs_symbol, year=2023, quarter=4)
            if rs.error_code != "0":
                return pd.DataFrame()
            while rs.next():
                data.append(rs.get_row_data())

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=rs.fields)
        df["symbol"] = symbol
        return df

    @staticmethod
    def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """标准化baostock数据格式，与akshare输出格式对齐"""
        # baostock字段名映射到标准字段名
        column_map = {
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
            "turn": "turnover",
            "pctChg": "pct_change",
        }

        # 重命名列
        existing_map = {k: v for k, v in column_map.items() if k in df.columns}
        df = df.rename(columns=existing_map)

        # 转换数值类型 (baostock返回字符串)
        numeric_cols = ["open", "high", "low", "close", "volume", "amount", "turnover", "pct_change"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 设置日期索引
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")

        # 添加股票代码 (使用原始6位代码)
        df["symbol"] = symbol

        return df