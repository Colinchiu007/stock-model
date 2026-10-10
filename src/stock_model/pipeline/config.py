"""
流水线配置

配置驱动的交易流水线参数，支持YAML文件加载。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PipelineConfig:
    """交易流水线配置

    所有参数均可通过构造函数或YAML配置文件设置。

    Attributes:
        watchlist: 监控股票列表
        data_source: 数据源 ("akshare" / "baostock")
        start_date: 数据起始日期 (YYYYMMDD)
        min_data_rows: 最少数据行数(低于此数跳过分析)
        min_quality_score: 最低数据质量评分(0-1, 低于此数跳过)
        signal_cooldown_minutes: 信号冷却期(分钟, 同一股票同一操作在此时间内不重复)
        initial_capital: 初始资金
        max_position_pct: 单股最大仓位比例
        min_shares: 最小交易股数(A股100股)
        risk_max_drawdown: 最大回撤限制
        risk_position_concentration: 单股集中度限制
        risk_stop_loss_pct: 默认止损比例
        risk_take_profit_pct: 默认止盈比例
        position_method: 仓位计算方法 ("fixed" / "kelly" / "risk_parity")
        position_fixed_pct: 固定仓位比例(position_method="fixed"时使用)
        enable_notify: 是否启用信号推送
        enable_quality_check: 是否启用数据质量检查
        enable_risk_check: 是否启用风控检查
    """

    # 数据配置
    watchlist: list[str] = field(default_factory=lambda: ["000001", "600036"])
    data_source: str = "akshare"
    start_date: str = "20240101"
    min_data_rows: int = 60
    min_quality_score: float = 0.5

    # 信号配置
    signal_cooldown_minutes: int = 60

    # 资金配置
    initial_capital: float = 100000.0
    max_position_pct: float = 0.20
    min_shares: int = 100

    # 风控配置
    risk_max_drawdown: float = 0.15
    risk_position_concentration: float = 0.20
    risk_stop_loss_pct: float = 0.08
    risk_take_profit_pct: float = 0.20

    # 仓位配置
    position_method: str = "fixed"  # "fixed" / "kelly" / "risk_parity"
    position_fixed_pct: float = 0.10

    # 功能开关
    enable_notify: bool = True
    enable_quality_check: bool = True
    enable_risk_check: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> PipelineConfig:
        """从字典创建配置(忽略未知字段)"""
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

    @classmethod
    def from_yaml(cls, path: str) -> PipelineConfig:
        """从YAML文件加载配置"""
        import yaml

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    def to_dict(self) -> dict:
        """导出为字典"""
        from dataclasses import asdict

        return asdict(self)
