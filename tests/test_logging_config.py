"""测试 logging_config 的日志文件 sink 配置。"""

from loguru import logger

from logging_config import configure_level_file_logging


class TestConfigureLevelFileLogging:
    def test_returns_three_sink_ids_and_creates_dir(self, tmp_path):
        ids = configure_level_file_logging(tmp_path)
        assert len(ids) == 3
        assert tmp_path.is_dir()
        for name in ("warning.log", "error.log", "critical.log"):
            assert (tmp_path / name).exists()
        for sink_id in ids:
            logger.remove(sink_id)

    def test_writes_each_level_to_correct_file(self, tmp_path):
        ids = configure_level_file_logging(tmp_path)
        try:
            logger.warning("warn-marker-123")
            logger.error("error-marker-456")
            logger.critical("crit-marker-789")

            warning_text = (tmp_path / "warning.log").read_text(encoding="utf-8")
            error_text = (tmp_path / "error.log").read_text(encoding="utf-8")
            critical_text = (tmp_path / "critical.log").read_text(encoding="utf-8")

            assert "warn-marker-123" in warning_text
            assert "warn-marker-123" not in error_text
            assert "error-marker-456" in error_text
            assert "crit-marker-789" in critical_text
        finally:
            for sink_id in ids:
                logger.remove(sink_id)

    def test_format_contains_level_and_record_fields(self, tmp_path):
        ids = configure_level_file_logging(tmp_path)
        try:
            logger.error("fmt-marker")
            line = (tmp_path / "error.log").read_text(encoding="utf-8").splitlines()[-1]
            assert "ERROR" in line
            assert "fmt-marker" in line
            # 时间戳使用 {time:YYYY-MM-DD HH:mm:ss.SSS} 渲染
            assert line.split("|")[0].strip()[:4].isdigit()
        finally:
            for sink_id in ids:
                logger.remove(sink_id)

    def test_can_be_called_twice(self, tmp_path):
        ids1 = configure_level_file_logging(tmp_path)
        ids2 = configure_level_file_logging(tmp_path)
        assert len(ids2) == 3
        for sink_id in ids1 + ids2:
            logger.remove(sink_id)
