"""
数据质量监控器

检查数据缺失、异常值、数据新鲜度等质量问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from loguru import logger


@dataclass
class QualityIssue:
    """数据质量问题"""

    severity: str  # "error", "warning", "info"
    category: str  # "missing", "outlier", "staleness", "schema"
    message: str
    symbol: str = ""
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.category}: {self.message}"


@dataclass
class QualityReport:
    """数据质量报告"""

    symbol: str
    issues: list[QualityIssue] = field(default_factory=list)
    score: float = 100.0  # 0-100, 100=完美

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == "warning" for i in self.issues)

    def __str__(self) -> str:
        error_count = sum(1 for i in self.issues if i.severity == "error")
        warn_count = sum(1 for i in self.issues if i.severity == "warning")
        return (
            f"QualityReport({self.symbol}: score={self.score:.1f}, "
            f"errors={error_count}, warnings={warn_count})"
        )


class DataQualityMonitor:
    """数据质量监控器

    检查数据缺失、异常值、数据新鲜度等质量问题。

    使用示例:
        monitor = DataQualityMonitor()
        report = monitor.check(df, symbol="000001")
        print(report)
        for issue in report.issues:
            print(issue)
    """

    def __init__(
        self,
        outlier_std_threshold: float = 3.0,
        staleness_hours: int = 24,
        max_missing_pct: float = 0.05,
    ):
        self.outlier_std_threshold = outlier_std_threshold
        self.staleness_hours = staleness_hours
        self.max_missing_pct = max_missing_pct

    def check(self, df: pd.DataFrame, symbol: str = "") -> QualityReport:
        """执行全面数据质量检查

        Args:
            df: 行情数据
            symbol: 股票代码

        Returns:
            质量报告
        """
        report = QualityReport(symbol=symbol)

        if df is None or df.empty:
            report.issues.append(
                QualityIssue(
                    severity="error",
                    category="schema",
                    message="数据为空",
                    symbol=symbol,
                )
            )
            report.score = 0.0
            return report

        # 1. 缺失值检查
        self._check_missing(df, symbol, report)

        # 2. 异常值检查
        self._check_outlier(df, symbol, report)

        # 3. 数据新鲜度检查
        self._check_staleness(df, symbol, report)

        # 4. Schema检查
        self._check_schema(df, symbol, report)

        # 计算质量评分
        report.score = self._calculate_score(report)

        if report.issues:
            logger.debug(f"数据质量检查 {symbol}: {report}")

        return report

    def _check_missing(self, df: pd.DataFrame, symbol: str, report: QualityReport) -> None:
        """检查缺失值"""
        total_cells = df.shape[0] * df.shape[1]
        if total_cells == 0:
            return

        missing_count = df.isnull().sum().sum()
        missing_pct = missing_count / total_cells

        if missing_pct > self.max_missing_pct:
            report.issues.append(
                QualityIssue(
                    severity="error" if missing_pct > 0.1 else "warning",
                    category="missing",
                    message=f"缺失率={missing_pct:.2%} (阈值={self.max_missing_pct:.2%})",
                    symbol=symbol,
                    details={"missing_pct": float(missing_pct)},
                )
            )
        elif missing_count > 0:
            report.issues.append(
                QualityIssue(
                    severity="info",
                    category="missing",
                    message=f"轻微缺失: {missing_count}个单元格 ({missing_pct:.2%})",
                    symbol=symbol,
                )
            )

    def _check_outlier(self, df: pd.DataFrame, symbol: str, report: QualityReport) -> None:
        """检查异常值(基于Z-score)"""
        numeric_cols = df.select_dtypes(include=[np.number]).columns

        for col in numeric_cols:
            series = df[col].dropna()
            if len(series) < 10:
                continue

            mean = series.mean()
            std = series.std()
            if std == 0:
                continue

            z_scores = (series - mean).abs() / std
            outlier_count = (z_scores > self.outlier_std_threshold).sum()

            if outlier_count > 0:
                outlier_pct = outlier_count / len(series)
                report.issues.append(
                    QualityIssue(
                        severity="warning" if outlier_pct > 0.05 else "info",
                        category="outlier",
                        message=f"{col}: {outlier_count}个异常值 ({outlier_pct:.2%})",
                        symbol=symbol,
                        details={"column": col, "outlier_count": int(outlier_count)},
                    )
                )

    def _check_staleness(self, df: pd.DataFrame, symbol: str, report: QualityReport) -> None:
        """检查数据新鲜度"""
        if not isinstance(df.index, pd.DatetimeIndex):
            report.issues.append(
                QualityIssue(
                    severity="info",
                    category="staleness",
                    message="非时间索引，无法检查新鲜度",
                    symbol=symbol,
                )
            )
            return

        if len(df) == 0:
            return

        latest_date = df.index.max()
        now = datetime.now()
        age = now - latest_date.to_pydatetime()

        if age > timedelta(hours=self.staleness_hours):
            severity = "warning" if age > timedelta(hours=self.staleness_hours * 2) else "info"
            report.issues.append(
                QualityIssue(
                    severity=severity,
                    category="staleness",
                    message=(
                        f"数据过期: 最新日期={latest_date.strftime('%Y-%m-%d')}, 过期{age.days}天"
                    ),
                    symbol=symbol,
                    details={
                        "latest_date": str(latest_date),
                        "age_hours": age.total_seconds() / 3600,
                    },
                )
            )

    def _check_schema(self, df: pd.DataFrame, symbol: str, report: QualityReport) -> None:
        """检查数据Schema"""
        required_cols = {"close"}
        missing_cols = required_cols - set(df.columns)

        if missing_cols:
            report.issues.append(
                QualityIssue(
                    severity="error",
                    category="schema",
                    message=f"缺少必要列: {missing_cols}",
                    symbol=symbol,
                    details={"missing_columns": list(missing_cols)},
                )
            )

    def _calculate_score(self, report: QualityReport) -> float:
        """计算质量评分"""
        score = 100.0

        for issue in report.issues:
            if issue.severity == "error":
                score -= 20
            elif issue.severity == "warning":
                score -= 5
            elif issue.severity == "info":
                score -= 1

        return max(0.0, min(100.0, score))
