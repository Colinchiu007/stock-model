"""日志配置模块测试"""

from unittest.mock import MagicMock, patch

from loguru import logger

from stock_model.utils.logger import setup_logger


class TestSetupLogger:
    """setup_logger 配置测试"""

    def test_default_level_from_settings(self, tmp_path):
        """默认从settings读取日志级别"""
        mock_settings = MagicMock()
        mock_settings.log_level = "DEBUG"
        mock_settings.log_dir = tmp_path / "logs"

        with patch("stock_model.utils.logger.get_settings", return_value=mock_settings):
            # 移除现有handler避免冲突
            logger.remove()
            setup_logger()

        # 验证log_dir已创建
        assert (tmp_path / "logs").exists()

    def test_custom_level_overrides_settings(self, tmp_path):
        """自定义level参数覆盖settings"""
        mock_settings = MagicMock()
        mock_settings.log_level = "INFO"
        mock_settings.log_dir = tmp_path / "logs"

        with patch("stock_model.utils.logger.get_settings", return_value=mock_settings):
            logger.remove()
            setup_logger(level="WARNING")

        assert (tmp_path / "logs").exists()

    def test_log_dir_created_if_not_exists(self, tmp_path):
        """日志目录不存在时自动创建"""
        mock_settings = MagicMock()
        mock_settings.log_level = "INFO"
        mock_settings.log_dir = tmp_path / "deep" / "nested" / "logs"

        with patch("stock_model.utils.logger.get_settings", return_value=mock_settings):
            logger.remove()
            setup_logger()

        assert (tmp_path / "deep" / "nested" / "logs").exists()

    def test_setup_logger_idempotent(self, tmp_path):
        """多次调用setup_logger不报错"""
        mock_settings = MagicMock()
        mock_settings.log_level = "INFO"
        mock_settings.log_dir = tmp_path / "logs"

        with patch("stock_model.utils.logger.get_settings", return_value=mock_settings):
            logger.remove()
            setup_logger()
            setup_logger()

        assert (tmp_path / "logs").exists()
