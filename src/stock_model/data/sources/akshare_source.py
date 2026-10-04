"""Akshare 数据源实现

基于东方财富等公开数据源的免费A股数据接口。
注意: 东方财富的 push2/push2his 端点可能因反爬策略导致连接失败，
建议使用 baostock 作为备选数据源。
"""

from __future__ import annotations

import akshare as ak
import pandas as pd
from loguru import logger

from stock_model.data.sources.base import DataSource


class AkshareSource(DataSource):
    """Akshare 数据源

    基于东方财富等公开网站的数据接口，免费但可能受限流影响。
    """

    name = "akshare"

    def get_daily(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        logger.debug(f"[akshare] 获取日线: {symbol}")
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        return self._normalize(df, symbol)

    def get_weekly(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        logger.debug(f"[akshare] 获取周线: {symbol}")
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="weekly",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        return self._normalize(df, symbol)

    def get_monthly(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        logger.debug(f"[akshare] 获取月线: {symbol}")
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="monthly",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        return self._normalize(df, symbol)

    def get_realtime(self, symbol: str) -> pd.DataFrame:
        logger.debug(f"[akshare] 获取实时行情: {symbol}")
        df = ak.stock_zh_a_spot_em()
        df = df[df["代码"] == symbol]
        return df

    def get_stock_info(self, symbol: str) -> pd.DataFrame:
        logger.debug(f"[akshare] 获取股票信息: {symbol}")
        return ak.stock_individual_info_em(symbol=symbol)

    def get_valuation(self, symbol: str) -> pd.DataFrame:
        """获取估值数据(PE/PB/PS/ROE等)

        使用 ak.stock_a_indicator_lg 获取个股指标数据。
        """
        logger.debug(f"[akshare] 获取估值数据: {symbol}")
        df = ak.stock_a_indicator_lg(symbol=symbol)
        # 标准化列名
        column_map = {
            "trade_date": "date",
            "pe": "pe",
            "pe_ttm": "pe_ttm",
            "pb": "pb",
            "ps": "ps",
            "ps_ttm": "ps_ttm",
            "dv_ratio": "dv_ratio",
            "dv_ttm": "dv_ttm",
            "total_mv": "total_mv",
        }
        df = df.rename(columns=column_map)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        df["symbol"] = symbol
        return df

    def get_financial_summary(self, symbol: str) -> pd.DataFrame:
        """获取财务摘要数据

        使用 ak.stock_financial_abstract_ths 获取同花顺财务摘要。
        """
        logger.debug(f"[akshare] 获取财务摘要: {symbol}")
        try:
            df = ak.stock_financial_abstract_ths(symbol=symbol)
            df["symbol"] = symbol
            return df
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning(f"[akshare] 获取财务摘要网络失败(同花顺): {e}, 尝试备用接口")
            # 备用: 使用 stock_financial_analysis_indicator
            try:
                df = ak.stock_financial_analysis_indicator(symbol=symbol)
                df["symbol"] = symbol
                return df
            except (ConnectionError, TimeoutError, OSError) as e2:
                logger.error(f"[akshare] 获取财务摘要网络失败: {e2}")
                return pd.DataFrame()
        except Exception as e:
            logger.error(f"[akshare] 获取财务摘要失败: {e}")
            raise

    def get_sector_list(self) -> pd.DataFrame:
        logger.debug("[akshare] 获取行业板块列表")
        return ak.stock_board_industry_name_em()

    def get_sector_stocks(self, sector: str) -> pd.DataFrame:
        logger.debug(f"[akshare] 获取板块成分股: {sector}")
        return ak.stock_board_industry_cons_em(symbol=sector)

    @staticmethod
    def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """标准化akshare日线数据格式"""
        column_map = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "振幅": "amplitude",
            "涨跌幅": "pct_change",
            "涨跌额": "change",
            "换手率": "turnover",
        }
        df = df.rename(columns=column_map)

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")

        df["symbol"] = symbol
        return df
