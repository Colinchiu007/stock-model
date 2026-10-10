"""
全局配置管理

使用 pydantic-settings 管理配置，支持环境变量和 YAML 文件。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "config"


class DataSettings(BaseSettings):
    """数据相关配置"""

    model_config = SettingsConfigDict(env_prefix="STOCK_DATA_")

    # 数据存储路径
    raw_data_dir: Path = Field(default=DATA_DIR / "raw", description="原始数据目录")
    processed_data_dir: Path = Field(default=DATA_DIR / "processed", description="处理后数据目录")
    cache_dir: Path = Field(default=DATA_DIR / "cache", description="缓存目录")

    # 数据源配置
    default_source: str = Field(default="akshare", description="默认数据源: akshare/baostock")
    tushare_token: str | None = Field(default=None, description="Tushare API Token")

    # 请求配置
    request_timeout: int = Field(default=30, description="请求超时(秒)")
    retry_times: int = Field(default=3, description="重试次数")
    retry_delay: float = Field(default=1.0, description="重试间隔(秒)")

    # 缓存配置
    cache_enabled: bool = Field(default=True, description="是否启用数据缓存")
    cache_ttl: int = Field(default=3600, description="缓存有效期(秒), 默认1小时")

    # 代理配置
    proxy_url: str | None = Field(
        default=None,
        description="HTTP代理地址, 如 http://127.0.0.1:7897; 设为 'none' 禁用代理",
    )
    no_proxy: str | None = Field(
        default=None,
        description="不走代理的地址, 如 *.eastmoney.com,localhost",
    )


class AnalysisSettings(BaseSettings):
    """分析相关配置"""

    model_config = SettingsConfigDict(env_prefix="STOCK_ANALYSIS_")

    # 技术分析默认参数
    ma_periods: list[int] = Field(default=[5, 10, 20, 60, 120, 250], description="均线周期")
    macd_fast: int = Field(default=12, description="MACD快线周期")
    macd_slow: int = Field(default=26, description="MACD慢线周期")
    macd_signal: int = Field(default=9, description="MACD信号线周期")
    rsi_period: int = Field(default=14, description="RSI周期")
    boll_period: int = Field(default=20, description="布林带周期")
    boll_std: float = Field(default=2.0, description="布林带标准差倍数")

    # 基本面分析参数
    pe_max: float = Field(default=100.0, description="PE过滤上限")
    pe_min: float = Field(default=0.0, description="PE过滤下限")
    pb_max: float = Field(default=20.0, description="PB过滤上限")


class VisualizationSettings(BaseSettings):
    """可视化配置"""

    model_config = SettingsConfigDict(env_prefix="STOCK_VIZ_")

    default_engine: str = Field(default="plotly", description="默认绘图引擎: plotly/matplotlib")
    theme: str = Field(default="dark", description="主题: dark/light")
    width: int = Field(default=1200, description="图表宽度")
    height: int = Field(default=800, description="图表高度")
    export_format: str = Field(default="html", description="导出格式: html/png/svg/pdf")


class NotifySettings(BaseSettings):
    """推送/告警配置

    环境变量前缀 ``STOCK_NOTIFY_``，也可写在 ``.env`` 里。

    用途：把**系统级失败**（如模拟盘定时任务连续失败）主动推给用户，
    而不必等用户哪天想起来去看接口。默认只开控制台 —— 不配 webhook
    也至少能在日志里看到，配了则手机上能收到。
    """

    model_config = SettingsConfigDict(env_prefix="STOCK_NOTIFY_")

    enabled: bool = Field(default=True, description="总开关；false 时完全不推送告警")
    console: bool = Field(default=True, description="是否输出到控制台")
    file_path: str | None = Field(
        default=None, description="告警落盘文件(JSON 行), 如 data/paper/alerts.jsonl"
    )
    webhook_url: str | None = Field(
        default=None, description="Webhook 地址(钉钉/飞书/企业微信机器人), 含 token 属凭据"
    )
    webhook_timeout: int = Field(default=10, description="Webhook 超时(秒)")
    alert_every_n_failures: int = Field(
        default=3,
        description="连续失败时每 N 次再提醒一次(首次失败必提醒)；设 0 表示只提醒首次",
    )


class Settings(BaseSettings):
    """全局配置"""

    model_config = SettingsConfigDict(
        env_prefix="STOCK_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # 环境
    env: str = Field(default="development", description="运行环境")
    debug: bool = Field(default=False, description="调试模式")
    api_token: str | None = Field(
        default=None,
        description="仪表盘 API 访问令牌(STOCK_API_TOKEN)。"
        "未设置 = 不启用认证(本地/内网)；设置后 /api/* 全部要求 Bearer token"
        "(/api/health 与静态资源豁免)。把仪表盘暴露到公网前必须设置。",
    )

    # 子配置
    data: DataSettings = Field(default_factory=DataSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    visualization: VisualizationSettings = Field(default_factory=VisualizationSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)

    # 日志
    log_level: str = Field(default="INFO", description="日志级别")
    log_dir: Path = Field(default=PROJECT_ROOT / "logs", description="日志目录")


# 全局单例
_settings: Settings | None = None


def get_settings() -> Settings:
    """获取全局配置单例"""
    global _settings
    if _settings is None:
        _settings = Settings()
        # 确保必要目录存在
        _settings.data.raw_data_dir.mkdir(parents=True, exist_ok=True)
        _settings.data.processed_data_dir.mkdir(parents=True, exist_ok=True)
        _settings.data.cache_dir.mkdir(parents=True, exist_ok=True)
        _settings.log_dir.mkdir(parents=True, exist_ok=True)
    return _settings
