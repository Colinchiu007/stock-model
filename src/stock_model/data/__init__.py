"""数据获取与处理模块

支持多种数据源:
  - akshare: 免费A股数据 (默认, 基于东方财富)
  - baostock: 免费A股数据 (稳定备选, 基于证券宝, 不受反爬影响)
"""

from stock_model.data.fetcher import StockDataFetcher
from stock_model.data.processor import DataProcessor
from stock_model.data.sources import AkshareSource, BaostockSource, DataSource
from stock_model.data.storage import DataStorage

__all__ = [
    "StockDataFetcher",
    "DataProcessor",
    "DataStorage",
    "DataSource",
    "AkshareSource",
    "BaostockSource",
]
