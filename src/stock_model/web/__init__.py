"""
Web Dashboard 模块

基于 FastAPI 的 Web 仪表盘。
依赖 fastapi 和 uvicorn (可选，未安装时降级)。
"""

from stock_model.web.app import create_app

__all__ = ["create_app"]