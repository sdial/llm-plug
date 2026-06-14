from pathlib import Path

from loguru import logger


LOG_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)


def configure_level_file_logging(log_dir: Path | str) -> list[int]:
    """Configure the standard warning/error/critical loguru file sinks."""
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)
    return [
        logger.add(
            log_path / "warning.log",
            level="WARNING",
            rotation="10 MB",
            filter=lambda r: r["level"].name == "WARNING",
            encoding="utf-8",
            format=LOG_FILE_FORMAT,
        ),
        logger.add(
            log_path / "error.log",
            level="ERROR",
            rotation="10 MB",
            filter=lambda r: r["level"].name == "ERROR",
            encoding="utf-8",
            format=LOG_FILE_FORMAT,
        ),
        logger.add(
            log_path / "critical.log",
            level="CRITICAL",
            rotation="10 MB",
            filter=lambda r: r["level"].name == "CRITICAL",
            encoding="utf-8",
            format=LOG_FILE_FORMAT,
        ),
    ]
