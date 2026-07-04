from pathlib import Path

from loguru import logger


def setup_logger(log_dir: Path, level: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "forg1_monitor.log",
        level=level,
        rotation="10 MB",
        retention="14 days",
        enqueue=True,
        backtrace=True,
    )
