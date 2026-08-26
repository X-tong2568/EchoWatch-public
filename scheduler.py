# scheduler.py
"""EchoWatch 任务调度器 —— 管理所有定时任务的并发执行"""

import asyncio
import random
from datetime import datetime
from pathlib import Path

from bili_client import BiliClient
from config import Config, UpConfig
from database import Database
from email_notifier import Notifier
from logger_config import logger
from monitor_scene1 import Scene1Monitor
from monitor_scene2 import Scene2Monitor
from monitor_scene3 import Scene3Monitor
from monitor_scene4 import Scene4Monitor
from pinned_dynamic_monitor import check_pinned_dynamic, get_current_pinned_id, sync_pinned_id_to_config


class Scheduler:
    """
    统一异步调度器。

    所有任务都是 asyncio.Task，各自独立循环。
    每个循环的错误被隔离 —— 单个任务异常不影响其他任务。
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        client: BiliClient,
        scene1: Scene1Monitor,
        scene2: Scene2Monitor,
        scene3: Scene3Monitor,
        scene4: Scene4Monitor,
        notifier: Notifier,
        screenshotter=None,
    ):
        self.config = config
        self.db = db
        self.client = client
        self.scene1 = scene1
        self.scene2 = scene2
        self.scene3 = scene3
        self.scene4 = scene4
        self.notifier = notifier
        self.screenshotter = screenshotter  # 截图失败补截用（发送前兜底）

        self.running = True
        self._digest_sent_today = False  # 防止同一天重复发日报
        self._tasks: list = []  # 记录所有调度任务，供 stop() 统一取消

    # ==========================================================
    # 主入口
    # ==========================================================

    async def run(self):
        """启动所有调度任务"""
        tasks = []

        if self.config.monitor.scene1_enabled:
            tasks.append(asyncio.create_task(self._scene1_discover_loop()))
            tasks.append(asyncio.create_task(self._scene1_poll_loop()))
            tasks.append(asyncio.create_task(self._scene1_level2_poll_loop()))
            tasks.append(asyncio.create_task(self._scene1_priority_loop()))
            tasks.append(asyncio.create_task(self._scene1_relevel_loop()))
            tasks.append(asyncio.create_task(self._pinned_dynamic_check_loop()))
            tasks.append(asyncio.create_task(self._sub_comment_sweep_loop()))
            tasks.append(asyncio.create_task(self._priority_sub_batch_loop()))
            logger.info("场景一调度已启动 (发现+L1轮询+L2轮询+priority+重新分级+置顶检测+子评论基线扫查+子评论批量通知)")

        if self.config.monitor.scene2_enabled:
            tasks.append(asyncio.create_task(self._scene2_discover_loop()))
            tasks.append(asyncio.create_task(self._scene2_poll_loop()))
            tasks.append(asyncio.create_task(self._scene2_level2_poll_loop()))
            tasks.append(asyncio.create_task(self._scene2_relevel_loop()))
            tasks.append(asyncio.create_task(self._scene2_batch_notify_loop()))
            logger.info("场景二调度已启动 (发现+L1轮询+L2轮询+重新分级+批量通知)")

        if self.config.monitor.scene3_enabled and self.config.scene3.clip_up_list:
            tasks.append(asyncio.create_task(self._scene3_discover_loop()))
            tasks.append(asyncio.create_task(self._scene3_poll_loop()))
            tasks.append(asyncio.create_task(self._scene3_level2_poll_loop()))
            tasks.append(asyncio.create_task(self._scene3_relevel_loop()))
            tasks.append(asyncio.create_task(self._scene3_sweep_loop()))
            tasks.append(asyncio.create_task(self._scene3_batch_notify_loop()))
            logger.info("场景三调度已启动 (发现+L1轮询+L2轮询+重新分级+子评论基线扫查+批量通知)")

        if self.config.monitor.scene4_enabled and self.config.scene4.other_up_list:
            tasks.append(asyncio.create_task(self._scene4_discover_loop()))
            tasks.append(asyncio.create_task(self._scene4_poll_loop()))
            tasks.append(asyncio.create_task(self._scene4_level2_poll_loop()))
            tasks.append(asyncio.create_task(self._scene4_relevel_loop()))
            tasks.append(asyncio.create_task(self._scene4_sweep_loop()))
            tasks.append(asyncio.create_task(self._scene4_batch_notify_loop()))
            logger.info("场景四调度已启动 (发现+L1轮询+L2轮询+重新分级+子评论基线扫查+批量通知)")

        tasks.append(asyncio.create_task(self._level_transition_loop()))
        tasks.append(asyncio.create_task(self._daily_digest_loop()))
        tasks.append(asyncio.create_task(self._immediate_notify_loop()))
        tasks.append(asyncio.create_task(self._archive_cleanup_loop()))
        logger.info("通用调度任务已启动 (归档+日报+场景一即时通知+留档清理)")

        # 截图补截循环：场景一/二/三/四任一启用即启动
        # （入库暂缓的截图靠此循环分批补齐，否则邮件无原帖截图）
        if (self.config.monitor.scene1_enabled or self.config.monitor.scene2_enabled
                or self.config.monitor.scene3_enabled or self.config.monitor.scene4_enabled) and self.screenshotter:
            tasks.append(asyncio.create_task(self._screenshot_retry_loop()))
            logger.info(f"截图补截循环已启动: 每 {self.config.screenshot.retry_interval}s 执行")

        self._tasks = tasks
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"调度器异常: {e}")

    async def stop(self):
        """
        停止调度：置 running=False 并取消所有任务，等待其退出。
        调用方应在关闭数据库/HTTP会话之前调用，避免任务访问已关闭资源。
        """
        self.running = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # ==========================================================
    # 场景一 调度循环
    # ==========================================================

    async def _scene1_discover_loop(self):
        """场景一发现循环：定时发现新作品"""
        interval = self.config.intervals.discover_interval
        logger.info(f"场景一发现循环: 每 {interval}s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    await self.scene1.discover(up)
            except Exception as e:
                logger.error(f"场景一发现异常: {e}")
            await asyncio.sleep(interval)

    async def _scene1_poll_loop(self):
        """场景一轮询循环：轮询 Level 1 + priority 项（不含priority高频已覆盖的）"""
        interval = self.config.intervals.level1_poll_seconds
        logger.info(f"场景一轮询循环: 每 {interval}s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    await self.scene1.poll_all(up)
            except Exception as e:
                logger.error(f"场景一轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _scene1_level2_poll_loop(self):
        """场景一 L2 轮询循环：轮询 Level 2 作品（24~120h，低频）"""
        interval = self.config.intervals.level2_poll_seconds
        logger.info(f"场景一 L2 轮询: 每 {interval}s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    await self.scene1.poll_level2(up)
            except Exception as e:
                logger.error(f"场景一L2轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _scene1_priority_loop(self):
        """场景一 priority 高频轮询：仅轮询置顶/优先项，随机 1~5s 间隔防风控"""
        logger.info("场景一 priority 轮询: 随机 1~5s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    await self.scene1.poll_priority_only(up)
            except Exception as e:
                logger.error(f"priority轮询异常: {e}")
            await asyncio.sleep(random.uniform(1, 5))

    async def _sub_comment_sweep_loop(self):
        """
        子评论基线兜底扫查循环：不依赖主评论列表可见性，
        定期对基线表里的根评论直接用子评论 API 查 count，
        count 变大或基线过期时触发完整翻页检测（防静默漏检）。
        """
        interval = self.config.intervals.sub_sweep_interval
        logger.info(f"子评论基线扫查: 每 {interval}s 执行")
        while self.running:
            try:
                await self.scene1.sweep_sub_comment_baselines()
            except Exception as e:
                logger.error(f"子评论基线扫查异常: {e}")
            await asyncio.sleep(interval)

    async def _scene1_relevel_loop(self):
        """场景一重新分级循环：每5分钟检查活跃项年龄，触发 L1→L2→L0 流转"""
        interval = 300  # 5分钟
        logger.info(f"场景一重新分级: 每 {interval}s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    await self.scene1.recheck_all_levels(up)
            except Exception as e:
                logger.error(f"场景一重新分级异常: {e}")
            await asyncio.sleep(interval)

    async def _pinned_dynamic_check_loop(self):
        """
        置顶动态自动检测循环：定期检查 B站 置顶是否更换。

        - 发现置顶变更时：更新 DB priority 标记、发送邮件通知
        - 间隔由 config.intervals.pinned_check_interval 控制（默认1小时）
        """
        interval = self.config.intervals.pinned_check_interval
        logger.info(f"置顶动态检测: 每 {interval}s 检查")
        while self.running:
            try:
                for up in self.config.up_list:
                    result = await check_pinned_dynamic(self.client, up.uid)

                    if not result["changed"]:
                        continue

                    # ---- 置顶发生变更（仅当检测到新置顶ID时，新替旧）----
                    # 说明：check_pinned_dynamic 保证 changed=True 时 current_id 必为新置顶ID，
                    # 绝不出现"None 替换旧置顶"（space feed 置顶标签会间歇性漏检）
                    new_id = result["current_id"]
                    old_id = result["previous_id"]
                    is_new = result["is_new"]

                    # 新置顶入库：通过 detail API 获取 oid。
                    # _sync_priority_dynamics 内部会清除旧 priority 标记并立即重新分级（L1/L2 重算）
                    logger.info(
                        f"[{up.name}] 置顶变更新增: {new_id} "
                        f"({'首次' if is_new else '替换旧置顶 ' + str(old_id)})"
                    )
                    await self.scene1._sync_priority_dynamics(up)

                    # 同步写入 config.yaml（重启后兜底）
                    sync_pinned_id_to_config(up.uid, new_id)

                    # 发送置顶变更邮件通知
                    await self.notifier.send_pinned_change(
                        up.name, up.uid, new_id, old_id, is_new
                    )

            except Exception as e:
                logger.error(f"置顶动态检测异常: {e}")
            await asyncio.sleep(interval)

    # ==========================================================
    # 场景二 调度循环
    # ==========================================================

    async def _scene2_discover_loop(self):
        """场景二发现循环：定时拉取话题最新帖子"""
        interval = self.config.intervals.topic_poll_seconds
        logger.info(f"场景二发现循环: 每 {interval}s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    if up.topics:
                        await self.scene2.discover_topic_posts(up)
            except Exception as e:
                logger.error(f"场景二发现异常: {e}")
            await asyncio.sleep(interval)

    async def _scene2_poll_loop(self):
        """场景二 L1 轮询循环：轮询 Level 1 + 预测窗口项（高频）"""
        interval = self.config.intervals.level1_poll_seconds
        logger.info(f"场景二 L1 轮询: 每 {interval}s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    if up.topics:
                        await self.scene2.poll_all(up)
            except Exception as e:
                logger.error(f"场景二L1轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _scene2_level2_poll_loop(self):
        """场景二 L2 轮询循环：轮询 Level 2 帖子（24~120h，低频）"""
        interval = self.config.intervals.level2_poll_seconds
        logger.info(f"场景二 L2 轮询: 每 {interval}s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    if up.topics:
                        await self.scene2.poll_level2(up)
            except Exception as e:
                logger.error(f"场景二L2轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _scene2_relevel_loop(self):
        """场景二重新分级循环：每5分钟检查活跃项年龄，触发 L1→L2→L0 流转"""
        interval = 300  # 5分钟
        logger.info(f"场景二重新分级: 每 {interval}s 执行")
        while self.running:
            try:
                for up in self.config.up_list:
                    if up.topics:
                        await self.scene2.recheck_all_levels(up)
            except Exception as e:
                logger.error(f"场景二重新分级异常: {e}")
            await asyncio.sleep(interval)

    # ==========================================================
    # 通用任务
    # ==========================================================

    async def _level_transition_loop(self):
        """级别升降循环：每分钟检查超时归档"""
        interval = 60
        logger.info(f"级别检查循环: 每 {interval}s 执行")
        while self.running:
            try:
                # 归档超时项
                count = await self.db.archive_stale(self.config.thresholds.archive_hours)
                if count > 0:
                    logger.info(f"归档了 {count} 条超时监测项")
            except Exception as e:
                logger.error(f"归档检查异常: {e}")
            await asyncio.sleep(interval)

    async def _daily_digest_loop(self):
        """日报循环：每天在配置时间发送汇总"""
        logger.info(f"日报调度: 每天 {self.config.notify.daily_digest_hour}:00 发送")
        while self.running:
            try:
                now = datetime.now()
                target_hour = self.config.notify.daily_digest_hour

                # 凌晨重置标记
                if now.hour == 0:
                    self._digest_sent_today = False

                # 在目标小时发送（且今天没发过）
                if now.hour == target_hour and not self._digest_sent_today:
                    for up in self.config.up_list:
                        await self.notifier.send_daily_digest(up.name, up.uid)
                    self._digest_sent_today = True
            except Exception as e:
                logger.error(f"日报发送异常: {e}")
            await asyncio.sleep(60)  # 每分钟检查一次

    async def _immediate_notify_loop(self):
        """即时通知循环（仅场景一）：发现新互动逐条立即发送。
        priority 项的互动由 _poll_item 内联发送（更快），此处排除避免重复。
        非 priority 发送前兜底补截原帖截图（与场景二/三批量发送前兜底一致）。
        """
        interval = 30  # 每30秒扫一次
        logger.info(f"即时通知循环（场景一）: 每 {interval}s 检查")
        while self.running:
            try:
                # 获取 priority 项的 item_id 集合，用于排除（它们由 poll_priority_only 内联发送）
                priority_ids = await self.db.get_priority_item_ids()
                interactions = await self.db.get_unnotified_immediate(scene="scene1")
                # 发送前兜底：非 priority 互动的原帖若无截图文件则现场补截一次
                # （priority 属日常分享，通知不附原帖截图，跳过；文件已存在则零成本）
                non_priority = [
                    it for it in interactions
                    if it.get("item_id", "") not in priority_ids
                ]
                if self.screenshotter and non_priority:
                    await self._retry_screenshot_before_send(non_priority)
                for interaction in interactions:
                    # 跳过 priority 项的互动，避免与 _poll_item 内联发送竞争→重复邮件
                    if interaction.get("item_id", "") in priority_ids:
                        continue
                    up_name = self._get_up_name(interaction.get("up_uid", ""))
                    item_type = await self._get_item_type(interaction.get("item_id", ""))
                    await self.notifier.send_immediate(interaction, up_name, item_type)
                    await asyncio.sleep(random.uniform(1, 2))
            except Exception as e:
                logger.error(f"即时通知异常: {e}")
            await asyncio.sleep(interval)

    async def _priority_sub_batch_loop(self):
        """priority 子评论批量通知循环：合并为一封邮件发送（主评论仍走即时推送）"""
        interval = self.config.intervals.priority_sub_batch_seconds
        logger.info(f"priority子评论批量通知: 每 {interval}s 合并发送")
        while self.running:
            try:
                priority_ids = await self.db.get_priority_item_ids()
                if priority_ids:
                    for up in self.config.up_list:
                        await self.notifier.send_priority_sub_batch(up.name, up.uid)
            except Exception as e:
                logger.error(f"priority子评论批量通知异常: {e}")
            await asyncio.sleep(interval)

    async def _scene2_batch_notify_loop(self):
        """场景二批量通知循环：将队列中的互动合并为一封邮件发送"""
        interval = self.config.intervals.scene2_batch_seconds
        logger.info(f"场景二批量通知: 每 {interval}s 合并发送")
        while self.running:
            try:
                interactions = await self.db.get_unnotified_immediate(scene="scene2")
                if interactions:
                    # 发送前兜底：本次待发互动对应的原帖若无截图，现场补截一次
                    if self.screenshotter:
                        await self._retry_screenshot_before_send(interactions)
                    # 收集涉及的 UP主 名称
                    up_names = sorted({
                        self._get_up_name(it.get("up_uid", ""))
                        for it in interactions
                    })
                    up_name = "、".join(up_names)
                    await self.notifier.send_scene2_batch(up_name)
            except Exception as e:
                logger.error(f"场景二批量通知异常: {e}")
            await asyncio.sleep(interval)

    # ==========================================================
    # 场景三 调度循环
    # ==========================================================

    async def _scene3_discover_loop(self):
        """场景三发现循环：定时拉取切片员空间动态列表，发现新投稿"""
        interval = self.config.intervals.discover_interval
        logger.info(f"场景三发现循环: 每 {interval}s 执行")
        while self.running:
            try:
                for clip_up in self.config.scene3.clip_up_list:
                    await self.scene3.discover(clip_up)
            except Exception as e:
                logger.error(f"场景三发现异常: {e}")
            await asyncio.sleep(interval)

    async def _scene3_poll_loop(self):
        """场景三 L1 轮询循环：轮询 Level 1 切片视频（24h内，高频）"""
        interval = self.config.intervals.level1_poll_seconds
        logger.info(f"场景三 L1 轮询: 每 {interval}s 执行")
        while self.running:
            try:
                for clip_up in self.config.scene3.clip_up_list:
                    await self.scene3.poll_all(clip_up)
            except Exception as e:
                logger.error(f"场景三L1轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _scene3_level2_poll_loop(self):
        """场景三 L2 轮询循环：轮询 Level 2 切片视频（24~120h，低频）"""
        interval = self.config.intervals.level2_poll_seconds
        logger.info(f"场景三 L2 轮询: 每 {interval}s 执行")
        while self.running:
            try:
                for clip_up in self.config.scene3.clip_up_list:
                    await self.scene3.poll_level2(clip_up)
            except Exception as e:
                logger.error(f"场景三L2轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _scene3_relevel_loop(self):
        """场景三重新分级循环：每5分钟检查活跃项年龄，触发 L1→L2→L0 流转"""
        interval = 300  # 5分钟
        logger.info(f"场景三重新分级: 每 {interval}s 执行")
        while self.running:
            try:
                for clip_up in self.config.scene3.clip_up_list:
                    await self.scene3.recheck_all_levels(clip_up)
            except Exception as e:
                logger.error(f"场景三重新分级异常: {e}")
            await asyncio.sleep(interval)

    async def _scene3_sweep_loop(self):
        """场景三子评论基线兜底扫查循环：防主评论列表间歇漏检导致的静默漏检"""
        interval = self.config.intervals.sub_sweep_interval
        logger.info(f"场景三子评论基线扫查: 每 {interval}s 执行")
        while self.running:
            try:
                await self.scene3.sweep_sub_comment_baselines()
            except Exception as e:
                logger.error(f"场景三子评论基线扫查异常: {e}")
            await asyncio.sleep(interval)

    async def _scene3_batch_notify_loop(self):
        """场景三批量通知循环：将队列中的互动合并为一封邮件发送（同场景二）"""
        interval = self.config.intervals.scene2_batch_seconds
        logger.info(f"场景三批量通知: 每 {interval}s 合并发送")
        while self.running:
            try:
                interactions = await self.db.get_unnotified_immediate(scene="scene3")
                if interactions:
                    # 发送前兜底：本次待发互动对应的原帖若无截图，现场补截一次
                    if self.screenshotter:
                        await self._retry_screenshot_before_send(interactions)
                    # 收集涉及的 UP主 名称（互动记录 up_uid=目标UP主）
                    up_names = sorted({
                        self._get_up_name(it.get("up_uid", ""))
                        for it in interactions
                    })
                    up_name = "、".join(up_names)
                    await self.notifier.send_scene3_batch(up_name)
            except Exception as e:
                logger.error(f"场景三批量通知异常: {e}")
            await asyncio.sleep(interval)

    # ==========================================================
    # 场景四 调度循环
    # ==========================================================

    async def _scene4_discover_loop(self):
        """场景四发现循环：定时拉取其他UP空间动态列表，发现新帖子"""
        interval = self.config.intervals.discover_interval
        logger.info(f"场景四发现循环: 每 {interval}s 执行")
        while self.running:
            try:
                for other_up in self.config.scene4.other_up_list:
                    await self.scene4.discover(other_up)
            except Exception as e:
                logger.error(f"场景四发现异常: {e}")
            await asyncio.sleep(interval)

    async def _scene4_poll_loop(self):
        """场景四 L1 轮询循环：轮询 Level 1 帖子（24h内，高频）"""
        interval = self.config.intervals.level1_poll_seconds
        logger.info(f"场景四 L1 轮询: 每 {interval}s 执行")
        while self.running:
            try:
                for other_up in self.config.scene4.other_up_list:
                    await self.scene4.poll_all(other_up)
            except Exception as e:
                logger.error(f"场景四L1轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _scene4_level2_poll_loop(self):
        """场景四 L2 轮询循环：轮询 Level 2 帖子（24~120h，低频）"""
        interval = self.config.intervals.level2_poll_seconds
        logger.info(f"场景四 L2 轮询: 每 {interval}s 执行")
        while self.running:
            try:
                for other_up in self.config.scene4.other_up_list:
                    await self.scene4.poll_level2(other_up)
            except Exception as e:
                logger.error(f"场景四L2轮询异常: {e}")
            await asyncio.sleep(interval)

    async def _scene4_relevel_loop(self):
        """场景四重新分级循环：每5分钟检查活跃项年龄，触发 L1→L2→L0 流转"""
        interval = 300  # 5分钟
        logger.info(f"场景四重新分级: 每 {interval}s 执行")
        while self.running:
            try:
                for other_up in self.config.scene4.other_up_list:
                    await self.scene4.recheck_all_levels(other_up)
            except Exception as e:
                logger.error(f"场景四重新分级异常: {e}")
            await asyncio.sleep(interval)

    async def _scene4_sweep_loop(self):
        """场景四子评论基线兜底扫查循环：防主评论列表间歇漏检导致的静默漏检"""
        interval = self.config.intervals.sub_sweep_interval
        logger.info(f"场景四子评论基线扫查: 每 {interval}s 执行")
        while self.running:
            try:
                await self.scene4.sweep_sub_comment_baselines()
            except Exception as e:
                logger.error(f"场景四子评论基线扫查异常: {e}")
            await asyncio.sleep(interval)

    async def _scene4_batch_notify_loop(self):
        """场景四批量通知循环：将队列中的互动合并为一封邮件发送（同场景二/三）"""
        interval = self.config.intervals.scene2_batch_seconds
        logger.info(f"场景四批量通知: 每 {interval}s 合并发送")
        while self.running:
            try:
                interactions = await self.db.get_unnotified_immediate(scene="scene4")
                if interactions:
                    # 发送前兜底：本次待发互动对应的原帖若无截图，现场补截一次
                    if self.screenshotter:
                        await self._retry_screenshot_before_send(interactions)
                    # 收集涉及的 UP主 名称（互动记录 up_uid=目标UP主）
                    up_names = sorted({
                        self._get_up_name(it.get("up_uid", ""))
                        for it in interactions
                    })
                    up_name = "、".join(up_names)
                    await self.notifier.send_scene4_batch(up_name)
            except Exception as e:
                logger.error(f"场景四批量通知异常: {e}")
            await asyncio.sleep(interval)

    async def _screenshot_retry_loop(self):
        """截图补截循环：定期补截入库时截图失败的动态（screenshot_pending=1）"""
        interval = self.config.screenshot.retry_interval
        while self.running:
            try:
                await self.scene2.retry_screenshots()
            except Exception as e:
                logger.error(f"截图补截循环异常: {e}")
            await asyncio.sleep(interval)

    async def _archive_cleanup_loop(self):
        """
        留档清理循环：每日清理超过 archive_keep_days 天的留档截图 PNG。

        邮件HTML发送时已内嵌 base64 图片，PNG 仅发送前读取用，
        过期删除不影响已留档邮件，磁盘占用进入稳态封顶。
        """
        keep_days = self.config.screenshot.archive_keep_days
        interval = 24 * 3600  # 每日执行一次
        logger.info(f"留档清理循环: 每日执行, 保留 {keep_days} 天")
        while self.running:
            try:
                save_dir = Path(self.config.screenshot.save_dir)
                if not save_dir.exists():
                    await asyncio.sleep(interval)
                    continue
                cutoff = datetime.now().timestamp() - keep_days * 86400
                removed = 0
                for f in save_dir.glob("*.png"):
                    try:
                        if f.stat().st_mtime < cutoff:
                            f.unlink()
                            removed += 1
                    except OSError as e:
                        logger.warning(f"留档清理失败 {f.name}: {e}")
                if removed:
                    logger.info(f"留档清理: 删除 {removed} 个过期截图")
            except Exception as e:
                logger.error(f"留档清理异常: {e}")
            await asyncio.sleep(interval)

    async def _retry_screenshot_before_send(self, interactions: list):
        """
        发送前兜底：待发互动的原帖若无截图文件则现场补截一次。

        截图失败（风控/遮罩）通常是间歇性的，发送时离入库已过一段时间，
        补截成功率更高。失败不影响邮件发送（自动降级文本渲染）。
        """
        save_dir = Path(self.config.screenshot.save_dir)
        for it in interactions:
            item_id = it.get("item_id", "")
            if not item_id:
                continue
            if (save_dir / f"dynamic_{item_id}.jpeg").exists():
                continue
            try:
                await self.screenshotter.take_dynamic_screenshot(item_id)
                logger.info(f"发送前补截成功: {item_id}")
            except Exception as e:
                logger.warning(f"发送前补截失败 ({item_id}): {e}")

    # ==========================================================
    # 辅助方法
    # ==========================================================

    def _get_up_name(self, up_uid: str) -> str:
        """根据 UID 查找 UP主 名称"""
        for up in self.config.up_list:
            if up.uid == up_uid:
                return up.name
        return f"UID:{up_uid}"

    async def _get_item_type(self, item_id: str) -> str:
        """根据 item_id 查找 item_type（用于邮件链接生成）"""
        return await self.db.get_item_type(item_id)
