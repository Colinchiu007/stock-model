"""数据源模块

支持多种数据源:
  - akshare: 免费A股数据 (默认, 基于东方财富)
  - baostock: 免费A股数据 (稳定备选, 基于证券宝)
  - tushare: 需要Token (专业级)
"""

from stock_model.data.sources.akshare_source import AkshareSource
from stock_model.data.sources.baostock_source import BaostockSource
from stock_model.data.sources.base import DataSource

__all__ = ["AkshareSource", "BaostockSource", "DataSource"]
