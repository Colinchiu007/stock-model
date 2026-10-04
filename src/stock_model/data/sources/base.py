"""数据源抽象基类

所有数据源实现必须继承此类并实现所有抽象方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class DataSource(ABC):
    """数据源抽象基类"""

    name: str = "base"

    @abstractmethod
    def get_daily(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """获取日线行情数据

        Args:
            symbol: 股票代码, 如 "000001"
            start_date: 开始日期, 如 "20240101"
            end_date: 结束日期, 如 "20241231"
            adjust: 复权类型 qfq(前复权)/hfq(后复权)/""(不复权)

        Returns:
            DataFrame with columns: date, open, high, low, close, volume, amount
        """

    @abstractmethod
    def get_weekly(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """获取周线行情数据"""

    @abstractmethod
    def get_monthly(
        self,
        symbol: str,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """获取月线行情数据"""

    @abstractmethod
    def get_realtime(self, symbol: str) -> pd.DataFrame:
        """获取实时行情"""

    @abstractmethod
    def get_stock_info(self, symbol: str) -> pd.DataFrame:
        """获取股票基本信息"""

    @abstractmethod
    def get_valuation(self, symbol: str) -> pd.DataFrame:
        """获取估值数据(PE/PB/PS/ROE等)

        Args:
            symbol: 股票代码, 如 "000001"

        Returns:
            DataFrame with columns: date, pe, pb, ps, roe 等
        """

    @abstractmethod
    def get_financial_summary(self, symbol: str) -> pd.DataFrame:
        """获取财务摘要数据

        Args:
            symbol: 股票代码, 如 "000001"

        Returns:
            DataFrame with financial summary data
        """

    @abstractmethod
    def get_sector_list(self) -> pd.DataFrame:
        """获取行业板块列表"""

    @abstractmethod
    def get_sector_stocks(self, sector: str) -> pd.DataFrame:
        """获取板块成分股"""

    def normalize_symbol(self, symbol: str) -> str:
        """标准化股票代码格式

        子类可覆盖此方法以适配不同数据源的代码格式。
        默认实现保持原格式。
        """
        return symbol
