# config.py
"""EchoWatch 配置加载模块 —— 解析 config.yaml，提供类型化访问"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml


# ============================================================
# 配置数据类
# ============================================================

@dataclass
class UpConfig:
    """单个 UP主 的监测配置"""
    uid: str                              # B站 UID
    name: str                             # 昵称（邮件抬头用）
    topics: list[int] = field(default_factory=list)        # 关注的话题 ID
    priority_dynamics: list[str] = field(default_factory=list)  # 优先监测的动态（兜底用，自动发现失败时使用）


@dataclass
class MonitorConfig:
    """场景开关"""
    scene1_enabled: bool = True
    scene2_enabled: bool = True


@dataclass
class IntervalsConfig:
    """轮询间隔配置（秒）"""
    topic_poll_seconds: int = 480
    level1_poll_seconds: int = 120
    level2_poll_seconds: int = 600
    priority_poll_seconds: int = 10
    random_delay_min: int = 1
    random_delay_max: int = 3
    discover_interval: int = 600
    scene2_batch_seconds: int = 180     # 场景二批量发送间隔（3分钟）
    priority_sub_batch_seconds: int = 180  # priority子评论批量发送间隔（3分钟，像场景二合并一封）
    pinned_check_interval: int = 3600   # 置顶动态自动检测间隔（秒），1小时
    sub_sweep_interval: int = 600       # 子评论基线兜底扫查间隔（秒），10分钟
    sub_sweep_max_age: int = 1800       # 基线超过该秒数（默认30分钟）强制重新翻页，防基线误标导致的永久漏检


@dataclass
class ThresholdsConfig:
    """分级时间阈值（小时）"""
    level1_hours: int = 24
    level2_hours: int = 120
    archive_hours: int = 168


@dataclass
class RetryConfig:
    """重试策略"""
    max_attempts: int = 3
    retry_wait_seconds: int = 50
    consecutive_fail_limit: int = 5


@dataclass
class EmailConfig:
    """邮件配置"""
    smtp_server: str = "smtp.qq.com"
    smtp_port: int = 587
    use_tls: bool = True
    sender_email: str = ""
    sender_password: str = ""
    receiver_email: str = ""


@dataclass
class NotifyConfig:
    """通知模式"""
    immediate: bool = True
    daily_digest: bool = True
    daily_digest_hour: int = 22
    prediction_idle_minutes: int = 120


@dataclass
class DatabaseConfig:
    """数据库配置"""
    path: str = "monitor.db"


@dataclass
class ScreenshotConfig:
    """截图配置（场景二原帖截图嵌入邮件）"""
    enabled: bool = True           # 是否启用截图模式
    max_per_batch: int = 5         # 单批最多截几张（防止大量场景二时耗时过长）
    save_dir: str = "sent_emails"  # 截图保存目录
    retry_interval: int = 600      # 截图失败补截循环间隔（秒），失败动态自动重试补截


@dataclass
class LoggingConfig:
    """日志配置"""
    level: str = "INFO"
    file: str = "logs/echowatch.log"   # 日志统一放 logs/ 目录，避免散落在项目根目录


# ============================================================
# 配置加载器
# ============================================================

class Config:
    """
    加载 config.yaml，提供类型化属性访问。

    用法:
        config = Config("config.yaml")
        print(config.up_list[0].name)
        print(config.intervals.level1_poll_seconds)
    """

    def __init__(self, config_path: str = "config.yaml"):
        """加载并解析 YAML 配置文件"""
        self._base_dir = Path(config_path).parent.resolve()
        raw = self._load_yaml(config_path)

        # 解析各配置段
        self.up_list = self._parse_up_list(raw.get("up_list", []))
        self.monitor = MonitorConfig(**raw.get("monitor", {}))
        self.intervals = IntervalsConfig(**raw.get("intervals", {}))
        self.thresholds = ThresholdsConfig(**raw.get("thresholds", {}))
        self.retry = RetryConfig(**raw.get("retry", {}))
        self.email = EmailConfig(**raw.get("email", {}))
        self.notify = NotifyConfig(**raw.get("notify", {}))
        self.database = DatabaseConfig(**raw.get("database", {}))
        self.screenshot = ScreenshotConfig(**raw.get("screenshot", {}))
        self.logging = LoggingConfig(**raw.get("logging", {}))

        # 数据库路径：相对路径基于 config.yaml 所在目录解析
        db_path = Path(self.database.path)
        if not db_path.is_absolute():
            self.database.path = str(self._base_dir / db_path)

    # ----------------------------------------------------------
    # 私有方法
    # ----------------------------------------------------------

    def _load_yaml(self, path: str) -> dict:
        """读取 YAML 文件，文件不存在时给出明确错误"""
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {file_path.resolve()}")
        with open(file_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _parse_up_list(self, raw_list: list) -> list[UpConfig]:
        """解析 UP主 列表，校验必填字段"""
        result = []
        for i, item in enumerate(raw_list):
            if not isinstance(item, dict):
                raise ValueError(f"up_list[{i}] 必须是字典类型")
            uid = item.get("uid")
            name = item.get("name")
            if not uid:
                raise ValueError(f"up_list[{i}] 缺少必填字段 'uid'")
            if not name:
                raise ValueError(f"up_list[{i}] 缺少必填字段 'name'")
            result.append(UpConfig(
                uid=str(uid),
                name=name,
                topics=[int(t) for t in item.get("topics", [])],
                priority_dynamics=[str(d) for d in item.get("priority_dynamics", [])],
            ))
        return result


# ============================================================
# 快速测试入口
# ============================================================

if __name__ == "__main__":
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.example.yaml"
    try:
        cfg = Config(config_path)
        print(f"[OK] 配置加载成功")
        print(f"   UP主数量: {len(cfg.up_list)}")
        for up in cfg.up_list:
            print(f"   - {up.name} (UID: {up.uid}) 话题: {up.topics} 优先动态: {up.priority_dynamics}")
        print(f"   DB路径: {cfg.database.path}")
        print(f"   场景一: {'开' if cfg.monitor.scene1_enabled else '关'}")
        print(f"   场景二: {'开' if cfg.monitor.scene2_enabled else '关'}")
        print(f"   Level1间隔: {cfg.intervals.level1_poll_seconds}s / Level2间隔: {cfg.intervals.level2_poll_seconds}s")
    except Exception as e:
        print(f"[FAIL] 配置加载失败: {e}")
        sys.exit(1)
