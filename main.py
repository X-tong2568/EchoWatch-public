#!/usr/bin/env python3
# main.py
"""EchoWatch（留声）— 主入口，组装所有模块并启动调度"""

import asyncio
import signal
import sys

from bili_client import BiliClient
from config import Config
from database import Database
from email_notifier import Notifier
from logger_config import logger, setup_logging
from monitor_scene1 import Scene1Monitor
from monitor_scene2 import Scene2Monitor
from monitor_scene3 import Scene3Monitor
from monitor_scene4 import Scene4Monitor
from scheduler import Scheduler
from screenshotter import Screenshotter


async def main():
    """主函数：初始化 → 验证 → 启动调度"""

    # ----------------------------------------------------------
    # 1. 加载配置
    # ----------------------------------------------------------
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    config = Config(config_path)
    logger.info(f"配置文件: {config_path}")

    # ----------------------------------------------------------
    # 2. 初始化日志
    # ----------------------------------------------------------
    setup_logging(config.logging.level, config.logging.file)
    logger.info("=" * 50)
    logger.info("EchoWatch（留声）启动中...")
    logger.info("=" * 50)

    # ----------------------------------------------------------
    # 3. 初始化数据库
    # ----------------------------------------------------------
    db = await Database(config.database.path).initialize()
    logger.info(f"数据库: {config.database.path}")

    # ----------------------------------------------------------
    # 4. 初始化 B站 API 客户端（零Cookie，纯公开API+WBI签名）
    # ----------------------------------------------------------
    client = BiliClient(
        breaker_cooldown_seconds=config.breaker.ratelimit_cooldown_seconds,
        comment_direct=config.comment.direct,
    )

    # ----------------------------------------------------------
    # 5. 验证 UP主 信息（场景三：自动获取切片员昵称，config 只需填 UID）
    # ----------------------------------------------------------
    for up in config.up_list:
        try:
            info = await client.get_user_info(up.uid)
            logger.info(f"UP主: {info.get('name', up.name)} (UID: {up.uid})")
        except Exception as e:
            logger.warning(f"获取 UP主 信息失败 (UID={up.uid}): {e}")

    # 场景三：切片员昵称自动获取（失败则保留 config 中的占位名，不阻塞启动）
    for clip_up in config.scene3.clip_up_list:
        try:
            info = await client.get_user_info(clip_up.uid)
            clip_up.name = info.get("name", clip_up.name)
            logger.info(f"切片员: {clip_up.name} (UID: {clip_up.uid})")
        except Exception as e:
            logger.warning(f"获取切片员信息失败 (UID={clip_up.uid}): {e}")

    # 场景四：其他UP昵称自动获取（同场景三，config 只需填 UID）
    for other_up in config.scene4.other_up_list:
        try:
            info = await client.get_user_info(other_up.uid)
            other_up.name = info.get("name", other_up.name)
            logger.info(f"其他UP: {other_up.name} (UID: {other_up.uid})")
        except Exception as e:
            logger.warning(f"获取其他UP信息失败 (UID={other_up.uid}): {e}")

    # ----------------------------------------------------------
    # 6. 初始化截图器（场景一/二/三/四原帖截图，在各场景监控器之前创建）
    # ----------------------------------------------------------
    screenshotter = None
    if (config.monitor.scene1_enabled or config.monitor.scene2_enabled
            or config.monitor.scene3_enabled or config.monitor.scene4_enabled) and config.screenshot.enabled:
        try:
            screenshotter = Screenshotter(config.screenshot.save_dir)
            await screenshotter.start()
            logger.info("截图器已就绪")
        except Exception as e:
            logger.warning(f"截图器启动失败，场景二将使用文本模式: {e}")
            screenshotter = None

    # ----------------------------------------------------------
    # 7. 初始化各模块
    # ----------------------------------------------------------
    notifier = Notifier(config, db)
    scene1 = Scene1Monitor(db, client, config, notifier, screenshotter)
    scene2 = Scene2Monitor(db, client, config, screenshotter)
    scene3 = Scene3Monitor(db, client, config, screenshotter)
    scene4 = Scene4Monitor(db, client, config, screenshotter)

    scheduler = Scheduler(config, db, client, scene1, scene2, scene3, scene4, notifier, screenshotter)

    # ----------------------------------------------------------
    # 8. 打印配置摘要
    # ----------------------------------------------------------
    logger.info("=" * 50)
    logger.info("配置摘要:")
    logger.info(f"  UP主数量: {len(config.up_list)}")
    for up in config.up_list:
        logger.info(f"    - {up.name} (UID: {up.uid})")
        logger.info(f"      话题: {up.topics}")
        logger.info(f"      优先动态: {len(up.priority_dynamics)} 个")
    logger.info(f"  场景一: {'启用' if config.monitor.scene1_enabled else '禁用'}")
    logger.info(f"  场景二: {'启用' if config.monitor.scene2_enabled else '禁用'}")
    logger.info(f"  场景三: {'启用' if config.monitor.scene3_enabled else '禁用'}")
    if config.monitor.scene3_enabled:
        logger.info(f"    目标UP主UID: {config.scene3.target_uid}")
        for clip_up in config.scene3.clip_up_list:
            logger.info(f"    切片员: {clip_up.name} (UID: {clip_up.uid})")
    logger.info(f"  场景四: {'启用' if config.monitor.scene4_enabled else '禁用'}")
    if config.monitor.scene4_enabled:
        logger.info(f"    目标UP主UID: {config.scene4.target_uid}")
        for other_up in config.scene4.other_up_list:
            logger.info(f"    其他UP: {other_up.name} (UID: {other_up.uid})")
    logger.info(f"  Level1 间隔: {config.intervals.level1_poll_seconds}s")
    logger.info(f"  Level2 间隔: {config.intervals.level2_poll_seconds}s")
    logger.info(f"  日报时间: {config.notify.daily_digest_hour}:00")
    logger.info(f"  即时通知: {'开' if config.notify.immediate else '关'}")
    logger.info("=" * 50)

    # ----------------------------------------------------------
    # 9. 注册信号处理（优雅退出）
    # ----------------------------------------------------------
    def shutdown():
        logger.info("收到退出信号，正在关闭...")
        scheduler.running = False

    try:
        signal.signal(signal.SIGINT, lambda s, f: shutdown())
        signal.signal(signal.SIGTERM, lambda s, f: shutdown())
    except (ValueError, AttributeError):
        # Windows 下 signal 支持有限，依赖 KeyboardInterrupt
        pass

    # ----------------------------------------------------------
    # 10. 启动调度器
    # ----------------------------------------------------------
    logger.info("启动调度器...")
    try:
        await scheduler.run()
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        # 先取消并等待所有调度任务退出，再关闭资源（避免任务访问已关闭的数据库）
        await scheduler.stop()
        await client.close()
        await db.close()
        if screenshotter:
            await screenshotter.stop()
        logger.info("EchoWatch 已退出")


if __name__ == "__main__":
    asyncio.run(main())
