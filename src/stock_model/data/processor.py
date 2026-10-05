"""
数据处理模块

功能:
  - 数据清洗
  - 缺失值处理
  - 数据对齐
  - 收益率计算
  - 数据合并
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

if TYPE_CHECKING:
    import pandas as pd


class DataProcessor:
    """数据处理器"""

    @staticmethod
    def clean(df: pd.DataFrame, remove_nan: bool = True) -> pd.DataFrame:
        """
        数据清洗

        Args:
            df: 原始数据
            remove_nan: 是否移除NaN行

        Returns:
            清洗后的DataFrame
        """
        df = df.copy()
        # 厠除法零值
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        for col in numeric_cols:
            if col in ["volume", "amount"]:
                df[col] = df[col].replace(0, np.nan)

        if remove_nan:
            before = len(df)
            df = df.dropna()
            logger.debug(f"数据清洗: {before} -> {len(df)} 条记录")

        return df

    @staticmethod
    def calculate_returns(df: pd.DataFrame, column: str = "close") -> pd.DataFrame:
        """
        计算收益率

        Args:
            df: 行情数据
            column: 计算基准列

        Returns:
            添加了收益率列的DataFrame
        """
        df = df.copy()
        df["return"] = df[column].pct_change()
        df["log_return"] = np.log(df[column] / df[column].shift(1))
        df["cum_return"] = (1 + df["return"]).cumprod() - 1
        return df

    @staticmethod
    def align_multiple(
        data_dict: dict[str, pd.DataFrame],
        method: str = "inner",
    ) -> dict[str, pd.DataFrame]:
        """
        对齐多只股票数据 (统一日期范围)

        Args:
            data_dict: {symbol: DataFrame}
            method: 对齐方式 inner/outer

        Returns:
            对齐后的数据字典
        """
        if not data_dict:
            return data_dict

        # 找到共同的日期范围
        all_dates = [df.index for df in data_dict.values()]
        if method == "inner":
            common_idx = all_dates[0]
            for idx in all_dates[1:]:
                common_idx = common_idx.intersection(idx)
        else:
            common_idx = all_dates[0]
            for idx in all_dates[1:]:
                common_idx = common_idx.union(idx)

        result = {}
        for symbol, df in data_dict.items():
            result[symbol] = df.loc[df.index.isin(common_idx)]

        logger.debug(f"数据对齐: {method}, 共 {len(common_idx)} 个交易日")
        return result

    @staticmethod
    def resample(df: pd.DataFrame, freq: str = "W") -> pd.DataFrame:
        """
        重采样行情数据

        Args:
            df: 日线数据
            freq: 目标频率 W(周)/M(月)/Q(季)

        Returns:
            重采样后的DataFrame
        """
        df = df.copy()
        ohlc_dict = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "amount": "sum",
        }
        # 只聚合存在的列
        agg_dict = {k: v for k, v in ohlc_dict.items() if k in df.columns}
        result = df.resample(freq).agg(agg_dict)
        return result.dropna()

    @staticmethod
    def normalize(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
        """
        数据标准化 (Z-score)

        Args:
            df: 原始数据
            columns: 需要标准化的列

        Returns:
            标准化后的DataFrame
        """
        df = df.copy()
        if columns is None:
            columns = df.select_dtypes(include=[np.number]).columns.tolist()

        for col in columns:
            if col in df.columns:
                mean = df[col].mean()
                std = df[col].std()
                if std != 0:
                    df[f"{col}_norm"] = (df[col] - mean) / std

        return df
