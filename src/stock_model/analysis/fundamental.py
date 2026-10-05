"""
基本面分析模块

功能:
  - 估值分析 (PE/PB/PS/ROE)
  - 财务指标分析
  - 成长性分析
  - 综合评分
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from loguru import logger

from stock_model.config.settings import get_settings
from stock_model.data.fetcher import StockDataFetcher


@dataclass
class FundamentalScore:
    """基本面评分结果"""

    symbol: str
    name: str = ""
    # 估值评分
    pe_score: float = 0.0
    pb_score: float = 0.0
    ps_score: float = 0.0
    # 盈利评分
    roe_score: float = 0.0
    profit_score: float = 0.0
    # 成长评分
    growth_score: float = 0.0
    # 综合评分
    total_score: float = 0.0
    # 原始数据
    raw_data: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"FundamentalScore({self.symbol}: "
            f"PE={self.pe_score:.1f}, PB={self.pb_score:.1f}, "
            f"ROE={self.roe_score:.1f}, Growth={self.growth_score:.1f}, "
            f"Total={self.total_score:.1f})"
        )


class FundamentalAnalysis:
    """基本面分析器"""

    def __init__(self, fetcher: StockDataFetcher | None = None):
        self.settings = get_settings().analysis
        self.fetcher = fetcher or StockDataFetcher()

    def get_valuation(self, symbol: str) -> pd.DataFrame:
        """
        获取估值数据

        Args:
            symbol: 股票代码

        Returns:
            估值数据DataFrame
        """
        logger.info(f"获取估值数据: {symbol}")
        return self.fetcher.get_valuation(symbol)

    def get_financial_summary(self, symbol: str) -> pd.DataFrame:
        """
        获取财务摘要

        Args:
            symbol: 股票代码

        Returns:
            财务摘要DataFrame
        """
        logger.info(f"获取财务摘要: {symbol}")
        return self.fetcher.get_financial_summary(symbol)

    def score_pe(self, pe: float) -> float:
        """
        PE评分 (0-100)

        PE越低越好，但需要排除负值和极端值
        0-15: 80-100分 (低估)
        15-30: 60-80分 (合理)
        30-50: 30-60分 (偏高)
        50+: 0-30分 (高估)
        """
        if pe <= 0:
            return 0.0
        if pe <= 15:
            return 80 + (15 - pe) / 15 * 20
        if pe <= 30:
            return 60 + (30 - pe) / 15 * 20
        if pe <= 50:
            return 30 + (50 - pe) / 20 * 30
        return max(0, 30 - (pe - 50) / 50 * 30)

    def score_pb(self, pb: float) -> float:
        """
        PB评分 (0-100)

        PB越低越好
        0-1: 80-100分
        1-3: 50-80分
        3-5: 20-50分
        5+: 0-20分
        """
        if pb <= 0:
            return 0.0
        if pb <= 1:
            return 80 + (1 - pb) * 20
        if pb <= 3:
            return 50 + (3 - pb) / 2 * 30
        if pb <= 5:
            return 20 + (5 - pb) / 2 * 30
        return max(0, 20 - (pb - 5) * 5)

    def score_roe(self, roe: float) -> float:
        """
        ROE评分 (0-100)

        ROE越高越好
        <5: 0-20分
        5-10: 20-40分
        10-15: 40-60分
        15-20: 60-80分
        20+: 80-100分
        """
        if roe <= 0:
            return 0.0
        if roe <= 5:
            return roe / 5 * 20
        if roe <= 10:
            return 20 + (roe - 5) / 5 * 20
        if roe <= 15:
            return 40 + (roe - 10) / 5 * 20
        if roe <= 20:
            return 60 + (roe - 15) / 5 * 20
        return min(100, 80 + (roe - 20) / 10 * 20)

    def analyze(self, symbol: str) -> FundamentalScore:
        """
        综合基本面分析

        Args:
            symbol: 股票代码

        Returns:
            基本面评分结果
        """
        logger.info(f"执行基本面分析: {symbol}")
        score = FundamentalScore(symbol=symbol)

        try:
            # 获取估值数据
            valuation_df = self.get_valuation(symbol)
            if valuation_df is not None and not valuation_df.empty:
                latest = valuation_df.iloc[-1]
                score.raw_data["valuation"] = latest.to_dict()

                # 提取PE/PB
                if "pe" in latest.index or "市盈率" in valuation_df.columns:
                    pe = latest.get("pe", latest.get("市盈率", 0))
                    if pe and not pd.isna(pe):
                        score.pe_score = self.score_pe(float(pe))

                if "pb" in latest.index or "市净率" in valuation_df.columns:
                    pb = latest.get("pb", latest.get("市净率", 0))
                    if pb and not pd.isna(pb):
                        score.pb_score = self.score_pb(float(pb))

        except (ValueError, KeyError, ConnectionError, RuntimeError) as e:
            logger.warning(f"获取估值数据失败: {e}")

        # 计算综合评分 (加权平均)
        weights = {
            "pe": 0.3,
            "pb": 0.2,
            "roe": 0.3,
            "growth": 0.2,
        }
        score.total_score = (
            score.pe_score * weights["pe"]
            + score.pb_score * weights["pb"]
            + score.roe_score * weights["roe"]
            + score.growth_score * weights["growth"]
        )

        logger.info(f"基本面分析完成: {score}")
        return score

    def batch_analyze(self, symbols: list[str]) -> list[FundamentalScore]:
        """
        批量基本面分析

        Args:
            symbols: 股票代码列表

        Returns:
            评分结果列表
        """
        results = []
        for symbol in symbols:
            try:
                score = self.analyze(symbol)
                results.append(score)
            except (ValueError, KeyError, TypeError) as e:
                logger.error(f"分析 {symbol} 失败: {e}")

        # 按综合评分排序
        results.sort(key=lambda x: x.total_score, reverse=True)
        return results
