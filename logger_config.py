# logger_config.py
"""EchoWatch 日志系统 —— 控制台 INFO + 文件 DEBUG，logs/ 独立目录，支持轮转（参考 BTCE 模式）"""

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


# 日志格式
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 旧日志保留天数（与 BTCE 一致，超过 3 天的日志文件启动时自动删除）
LOG_RETENTION_DAYS = 3

# 轮转参数（与 BTCE 一致：单文件 5MB，保留 3 个备份，主日志最多约 20MB）
MAX_LOG_SIZE_MB = 5
LOG_BACKUP_COUNT = 3

# 默认日志目录（项目根目录下 logs/，独立于代码文件，与 BTCE 的 logs/ 模式一致）
LOG_DIR = Path(__file__).parent / "logs"

# 全局 logger 实例，供其他模块直接导入使用
logger = logging.getLogger("EchoWatch")


def _cleanup_old_logs(log_dir: Path) -> list:
    """
    清理日志目录中超过保留天数（LOG_RETENTION_DAYS）的日志文件，与 BTCE 一致。

    按文件修改时间判断，超过 3 天直接删除；在创建新 handler 之前调用，
    避免文件句柄指向已删除文件。单个文件删除失败不影响整体。

    Args:
        log_dir: 日志目录

    Returns:
        被删除的文件名列表（含文件年龄，用于日志输出）
    """
    deleted = []
    if not log_dir.is_dir():
        return deleted
    now = time.time()
    # 匹配所有日志文件（含轮转备份 *.log.1 / *.log.2 等）
    for pattern in ("*.log", "*.log.*"):
        for log_file in log_dir.glob(pattern):
            try:
                if not log_file.is_file():
                    continue
                age_days = (now - log_file.stat().st_mtime) / (24 * 3600)
                if age_days > LOG_RETENTION_DAYS:
                    log_file.unlink()
                    deleted.append(f"{log_file.name} ({age_days:.1f}天前)")
            except OSError:
                continue
    return deleted


def setup_logging(log_level: str = "INFO", log_file: str = "logs/echowatch.log"):
    """
    初始化日志系统。

    Args:
        log_level: 控制台日志级别（DEBUG / INFO / WARNING / ERROR）
        log_file: 主日志文件路径（默认 logs/echowatch.log）
    """
    # 解析主日志路径（相对路径基于运行目录，由 config.yaml 传入）
    log_path = Path(log_file)
    # error.log 与主日志同目录，单独记录 ERROR 级别（方便排查风控等错误）
    error_path = log_path.parent / "error.log"

    # 确保日志目录存在
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 先清理超过 3 天的旧日志（与 BTCE 一致，防止旧备份长期堆积）
    deleted = _cleanup_old_logs(log_path.parent)

    # 获取根 logger
    root_logger = logging.getLogger("EchoWatch")
    root_logger.setLevel(logging.DEBUG)  # 允许最详细级别，由 handler 各自控制

    # 清除已有 handler（避免重复添加）
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 格式化器
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # 控制台 handler —— INFO 及以上
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(_parse_level(log_level))
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 主日志文件 handler —— DEBUG 及以上，按大小轮转
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 错误日志 handler —— 仅 ERROR 及以上，按大小轮转（与 BTCE 的 error.log 一致）
    error_handler = RotatingFileHandler(
        error_path,
        maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    root_logger.info(f"日志系统初始化完成 - 级别: {log_level}, 文件: {log_file}")
    if deleted:
        root_logger.info(f"已清理 {len(deleted)} 个超期日志文件: {', '.join(deleted)}")


def _parse_level(level_str: str) -> int:
    """将字符串日志级别转为 logging 常量"""
    return getattr(logging, level_str.upper(), logging.INFO)


# 模块导入时自动输出（非阻塞，方便排查）
if __name__ != "__main__":
    pass  # 等待 setup_logging() 调用后才正式开始输出
