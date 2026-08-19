# monitor_scene2.py
"""EchoWatch 场景二：监测指定话题下 UP主 在粉丝帖子中的互动"""

import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional

from bili_client import BiliClient, SUB_COMMENT_PAGE_SIZE, parse_comment, parse_sub_comment
from config import Config, UpConfig
from database import Database
from logger_config import logger

MAX_COMMENT_PAGES = 3
# 子评论检测节流间隔（秒）：同一根评论 rcount 变大后，距上次翻页 ≥ 此值才再翻
SUB_COMMENT_MIN_INTERVAL = 120
PREDICTION_WINDOW_SIZE = 20  # 预测窗口：最新 N 条帖子
PREDICTION_IDLE_CLEAR_MINUTES = 120  # 预测窗口无互动后自动清空


class Scene2Monitor:
    """
    场景二：监测指定话题下 UP主 在粉丝帖子中的互动。

    核心区别：
    - 仅匹配 UP主 亲自发的评论/回复（mid == up_uid）
    - 不追踪 up_action.like（那是发帖粉丝的态度）

    两阶段轮询：
    - 阶段A：拉取话题最新动态列表 → 新帖子入库
    - 阶段B：轮询监测队列中各帖子的评论区

    预测窗口机制：
    - UP 按最新顺序浏览帖子，从新到旧互动
    - 检测到互动后，该位置之前的 N 条帖子标记为预测热点
    - 热点帖子即使超过 24h 仍保持 Level 1
    - 超过 PREDICTION_IDLE_CLEAR_MINUTES 无互动则清空窗口
    """

    def __init__(self, db: Database, client: BiliClient, config: Config, screenshotter=None):
        self.db = db
        self.client = client
        self.config = config
        self.screenshotter = screenshotter  # 可选，入库时截图用
        self._polling_l1 = False
        self._polling_l2 = False
        self._disabled_until: dict = {}  # item_id → 跳过轮询截止时间（评论已关闭的帖子）

    # ==========================================================
    # 阶段A：发现话题新帖
    # ==========================================================

    async def discover_topic_posts(self, up: UpConfig):
        """
        拉取话题最新动态列表，发现新帖子并加入监测队列。

        翻页策略：从最新页开始逐页翻，直到遇到已入库的帖子或翻完3页。
        所有帖子默认 Level 1。
        """
        MAX_DISCOVER_PAGES = 3  # 每次发现最多翻3页（60条动态）

        for topic_id in up.topics:
            new_count = 0
            offset = ""  # 首次不传游标，从最新开始
            stop_paging = False

            for page in range(MAX_DISCOVER_PAGES):
                if stop_paging:
                    break

                try:
                    cards_resp = await self.client.get_topic_cards(topic_id, offset=offset)
                except Exception as e:
                    logger.error(f"获取话题动态失败 (topic={topic_id}): {e}")
                    break

                items = cards_resp.get("items", [])
                for card in items:
                    dyn_id = card.get("dynamic_id", "")
                    comment_oid = card.get("comment_oid", "")
                    dyn_type = card.get("type", "")
                    pub_ts = card.get("pub_ts", 0)
                    post_content = card.get("content", "")
                    post_rich = card.get("rich_content", "")

                    if not dyn_id or not comment_oid:
                        continue

                    # 跨场景去重：同一评论区（同一视频aid/动态）已被其他场景占用则跳过
                    # （如场景三切片已监测该视频，避免双份监测与场景归属混乱）
                    existing = await self.db.get_item_by_comment_oid(comment_oid)
                    if existing and existing["item_id"] != dyn_id:
                        logger.debug(
                            f"话题帖评论区已被监测 (oid={comment_oid} item={existing['item_id']})，跳过 {dyn_id}"
                        )
                        continue

                    inserted = await self.db.upsert_item(
                        item_id=dyn_id,
                        comment_oid=comment_oid,
                        item_type=dyn_type,
                        source="scene2",
                        up_uid=up.uid,
                        topic_id=topic_id,
                        pub_ts=pub_ts,
                        post_content=post_content,
                        post_rich_content=post_rich,
                    )
                    if inserted:
                        new_count += 1
                        # 入库时截取原帖截图（异步，失败不阻塞发现流程）
                        if self.screenshotter:
                            try:
                                shot_path = await self.screenshotter.take_dynamic_screenshot(dyn_id)
                                if shot_path is None:
                                    # 截图失败（风控/登录遮罩/浏览器异常）：标记待补截，由补截循环重试
                                    await self.db.mark_screenshot_pending(dyn_id)
                                    logger.warning(f"入库截图未成功，已标记待补截 ({dyn_id})")
                            except Exception as e:
                                await self.db.mark_screenshot_pending(dyn_id)
                                logger.warning(f"入库截图失败，已标记待补截 ({dyn_id}): {e}")
                    else:
                        # 遇到已入库的帖子 → 停止翻页（说明追上了历史）
                        # 注意：不能用 break 中断本页遍历 —— 若某条动态曾被 continue 跳过
                        # （接口字段波动/跨场景去重），它会排在已入库条目之后；
                        # break 会导致它永远不被补检（后续每轮第一页第一条即已入库 → 直接停）。
                        # 改为 continue：本页剩余条目全部检查完，仅阻止翻页，
                        # 保证漏检条目只要还在本页就能被补抓入库。
                        stop_paging = True
                        continue

                # 取下一页的 offset；无更多数据则停止
                offset = cards_resp.get("offset", "")
                if not cards_resp.get("has_more") or not offset:
                    break

            if new_count > 0:
                logger.info(f"[{up.name}] 话题 {topic_id} 发现 {new_count} 个新帖")

    async def retry_screenshots(self):
        """
        补截待截图的动态：查询 screenshot_pending=1 的条目逐个补截。

        单批最多 max_per_batch 张（限流），补截成功清除标记，失败保留等下轮。
        由 scheduler 的补截循环定时调用。
        """
        if not self.screenshotter:
            return
        try:
            pending_ids = await self.db.get_screenshot_pending_items(
                limit=self.config.screenshot.max_per_batch
            )
        except Exception as e:
            logger.error(f"查询待补截列表失败: {e}")
            return
        if not pending_ids:
            return
        for dyn_id in pending_ids:
            try:
                shot_path = await self.screenshotter.take_dynamic_screenshot(dyn_id)
                if shot_path:
                    await self.db.clear_screenshot_pending(dyn_id)
                    logger.info(f"补截成功 ({dyn_id})")
                else:
                    logger.warning(f"补截未成功，保留待补截标记 ({dyn_id})")
            except Exception as e:
                logger.warning(f"补截异常，保留待补截标记 ({dyn_id}): {e}")

    # ==========================================================
    # 阶段B：轮询帖子评论
    # ==========================================================

    async def poll_all(self, up: UpConfig):
        """
        轮询所有活跃话题帖子的评论区。

        合并来源：
        - Level 1 项（常规）
        - 预测窗口内的最新帖子（即使超过24h也保持 Level 1）
        """
        if self._polling_l1:
            logger.debug(f"[{up.name}] 场景二L1轮询跳过（上一轮未完成）")
            return
        self._polling_l1 = True
        try:
            await self._poll_all_impl(up)
        finally:
            self._polling_l1 = False

    async def _poll_all_impl(self, up: UpConfig):
        level1_items = await self.db.get_items_by_level(1, source="scene2")
        level1_ids = {it["item_id"] for it in level1_items}

        # 预测窗口：每个话题的最新 N 条帖子
        prediction_items = []
        for topic_id in up.topics:
            hot = await self.db.get_hot_items(up.uid, topic_id, limit=PREDICTION_WINDOW_SIZE)
            for it in hot:
                if it["item_id"] not in level1_ids:
                    prediction_items.append(it)
                    level1_ids.add(it["item_id"])

        items = level1_items + prediction_items

        logger.debug(
            f"[{up.name}] 场景二轮询: Level1={len(level1_items)}, "
            f"预测窗口={len(prediction_items)}, 合计={len(items)}"
        )

        for item in items:
            await asyncio.sleep(random.uniform(
                self.config.intervals.random_delay_min,
                self.config.intervals.random_delay_max,
            ))

            try:
                await self._poll_item(item, up)
            except Exception as e:
                logger.error(f"场景二轮询失败 item={item['item_id']}: {e}")

        # 检查预测窗口是否需要清空（长时间无互动）
        await self._check_prediction_clear(up)

    async def _poll_item(self, item: dict, up: UpConfig):
        """
        轮询单个帖子的评论区（仅匹配 mid == up_uid）。
        逻辑与场景一类似，但：
        - 不检查 up_action.like
        - 检测到互动后更新预测窗口
        """
        item_id = item["item_id"]
        comment_oid = item.get("comment_oid", "")
        dyn_type = item["item_type"]
        topic_id = item.get("topic_id")

        # 评论已关闭的帖子：跳过轮询（1小时后再试，避免无效请求风暴）
        until = self._disabled_until.get(item_id)
        if until and datetime.now() < until:
            return

        # 确定评论类型（使用客户端的统一映射）
        comment_type = self.client.get_comment_type(dyn_type)

        # 级别升降
        new_level = self._check_level_transition(item)
        if new_level != item["monitor_level"]:
            await self.db.set_level(item_id, new_level)
            if new_level == 0:
                return

        try:
            oid = int(comment_oid)
        except (ValueError, TypeError):
            logger.error(f"无效的 comment_oid: {comment_oid}")
            return

        # 拉取评论（游标翻页，按时间排序）
        pagination_str = ""  # 首次不传游标
        for page in range(MAX_COMMENT_PAGES):
            try:
                resp = await self.client.get_comments(
                    oid=oid,
                    comment_type=comment_type,
                    pagination_str=pagination_str,
                    mode=2,  # 按时间排序
                )
            except Exception as e:
                logger.warning(f"场景二评论API失败 page={page} oid={oid}: {e}")
                break

            # 评论功能已关闭/无评论（确定性错误）：标记跳过1小时
            if resp and resp.get("disabled"):
                self._disabled_until[item_id] = datetime.now() + timedelta(hours=1)
                break

            replies = resp.get("replies", []) if resp else []
            top_replies = resp.get("top_replies", []) if resp else []

            # 置顶评论（仅第一页有，需单独处理）
            for raw in top_replies:
                await self._process_comment(raw, oid, comment_type, item_id, up)

            if not replies:
                break

            for raw in replies:
                up_interacted = await self._process_comment(
                    raw, oid, comment_type, item_id, up
                )
                if up_interacted and topic_id:
                    # 更新预测窗口
                    await self.db.set_last_interacted(topic_id, item_id)
                    await self.db.set_up_interacted(item_id)

            # 游标翻页：优先看 is_end，其次看 pagination_reply.next_offset
            cursor = resp.get("cursor", {}) if resp else {}
            if cursor.get("is_end"):
                break
            pag_reply = cursor.get("pagination_reply", {}) if isinstance(cursor, dict) else {}
            pagination_str = pag_reply.get("next_offset", "")
            if not pagination_str or pagination_str == "no-next-offset":
                break

        await self.db.update_poll_time(item_id)

    async def _process_comment(self, raw: dict, oid: int,
                                comment_type, item_id: str, up: UpConfig) -> bool:
        """
        处理一级评论：仅匹配 UP主 自己的评论（mid == up_uid）。
        不检查 up_action.like。

        Returns:
            True 表示 UP主 在此帖互动过（用于更新预测窗口）
        """
        parsed = parse_comment(raw)
        rpid = parsed["rpid"]
        mid = parsed["mid"]
        content = parsed["content"]
        replies_count = parsed["replies_count"]

        up_found = False

        if mid == up.uid:
            up_found = True
            await self.db.insert_interaction({
                "up_uid": up.uid,
                "item_id": item_id,
                "comment_id": str(rpid),
                "is_sub_reply": False,
                "parent_content": None,
                "parent_author": None,
                "content": content,
                "rich_content": parsed.get("rich_content", ""),
                "up_liked": False,
                "scene": "scene2",
            })

        # 子评论（基线节流：首次见立即翻；rcount 变大 且 距上次翻 ≥ 2min 才翻）
        if replies_count > 0:
            baseline = await self.db.get_sub_baseline(item_id, rpid)
            should_check = True
            if baseline:
                gap_sec = float("inf")
                try:
                    last_dt = datetime.strptime(baseline["last_check_ts"], "%Y-%m-%d %H:%M:%S")
                    gap_sec = (datetime.now() - last_dt).total_seconds()
                except (ValueError, TypeError):
                    pass  # 时间解析失败 → 视为首次，直接翻
                should_check = (
                    replies_count > baseline["last_rcount"]
                    and gap_sec >= SUB_COMMENT_MIN_INTERVAL
                )

            if should_check:
                root_context = {
                    "rpid": rpid,
                    "uname": parsed["uname"],
                    "content": content,
                    "rich_content": parsed.get("rich_content", ""),
                }
                sub_found = await self._process_sub_comments(
                    oid, comment_type, rpid, item_id, up, root_context,
                    replies_count=replies_count,
                )
                if sub_found:
                    up_found = True

        return up_found

    async def _process_sub_comments(self, oid: int, comment_type,
                                     root_rpid, item_id: str, up: UpConfig,
                                     root_context: dict, replies_count: int = 0) -> bool:
        """
        拉取子评论，匹配 UP主 回复。
        与场景一相同的查找表法还原 parent 上下文。

        成功拿到子评论数据后更新检测基线（rcount 节流用），失败不更新。

        Returns:
            True 表示 UP主 在此处有互动
        """
        MAX_SUB_PAGES = 15  # 最多翻 15 页（ps=10 时约 150 条；不满页即停，超过上限的极端大楼靠 sweep 兜底）

        all_subs = []
        for page in range(1, MAX_SUB_PAGES + 1):
            try:
                resp = await self.client.get_sub_comments(
                    oid=oid, comment_type=comment_type,
                    root_rpid=root_rpid, page_index=page,
                )
            except Exception as e:
                logger.debug(f"场景二子评论API失败 root={root_rpid} page={page}: {e}")
                break

            subs = resp.get("replies") or [] if resp else []
            if not subs:
                break
            all_subs.extend(subs)

            # 检查是否还有更多页：不满一页即为末页（不依赖 count，防 count 虚高/滞后）
            if len(subs) < SUB_COMMENT_PAGE_SIZE:
                break

        if not all_subs:
            return False

        # 成功拿到子评论 → 更新检测基线（只覆盖当前值，不保留历史）
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.db.upsert_sub_baseline(item_id, root_rpid, replies_count, now_str)

        sub_lookup = {}
        for raw in all_subs:
            parsed = parse_sub_comment(raw)
            sub_lookup[parsed["rpid"]] = {
                "uname": parsed["uname"],
                "content": parsed["content"],
                "rich_content": parsed.get("rich_content", ""),
            }

        up_found = False
        for raw in all_subs:
            parsed = parse_sub_comment(raw)
            if parsed["mid"] != up.uid:
                continue

            up_found = True
            parent_rpid = parsed["parent_rpid"]
            if parent_rpid == root_rpid:
                parent_content = root_context["content"]
                parent_author = root_context["uname"]
                parent_rich = root_context.get("rich_content", "")
            elif parent_rpid in sub_lookup:
                parent_content = sub_lookup[parent_rpid]["content"]
                parent_author = sub_lookup[parent_rpid]["uname"]
                parent_rich = sub_lookup[parent_rpid]["rich_content"]
            else:
                parent_content = ""
                parent_author = ""
                parent_rich = ""

            await self.db.insert_interaction({
                "up_uid": up.uid,
                "item_id": item_id,
                "comment_id": str(parsed["rpid"]),
                "is_sub_reply": True,
                "parent_content": parent_content,
                "parent_author": parent_author,
                "parent_rich_content": parent_rich,
                "content": parsed["content"],
                "rich_content": parsed.get("rich_content", ""),
                "up_liked": False,
                "scene": "scene2",
            })

        return up_found

    # ==========================================================
    # 级别升降（与场景一相同逻辑，但阈值可能不同）
    # ==========================================================

    async def poll_level2(self, up: UpConfig):
        """
        低频轮询 Level 2 话题帖子（24~120h 的旧帖）。
        不包含预测窗口（Level 2 已过热点期）。
        """
        if self._polling_l2:
            logger.debug(f"[{up.name}] 场景二L2轮询跳过（上一轮未完成）")
            return
        self._polling_l2 = True
        try:
            level2_items = await self.db.get_items_by_level(2, source="scene2")
            if not level2_items:
                return

            logger.debug(f"[{up.name}] 场景二L2轮询: {len(level2_items)} 个")

            for item in level2_items:
                await asyncio.sleep(random.uniform(
                    self.config.intervals.random_delay_min,
                    self.config.intervals.random_delay_max,
                ))
                try:
                    await self._poll_item(item, up)
                except Exception as e:
                    logger.error(f"场景二L2轮询失败 item={item['item_id']}: {e}")
        finally:
            self._polling_l2 = False

    async def recheck_all_levels(self, up: UpConfig):
        """
        定期重新分级：遍历所有活跃的 scene2 监测项，按 first_seen_at 重新算级别。
        确保不依赖轮询也能正常 L1→L2→L0 流转。
        """
        items = await self.db.get_active_items_by_source("scene2")
        changed = 0
        for item in items:
            new_level = self._check_level_transition(item)
            if new_level != item["monitor_level"]:
                await self.db.set_level(item["item_id"], new_level)
                changed += 1
        if changed > 0:
            logger.info(f"[{up.name}] 场景二重新分级: {changed} 条变更")

    def _check_level_transition(self, item: dict) -> int:
        """
        根据时间阈值判断监测级别。
        - priority 项永远保持 Level 1
        - Level 1 （0~24h）→ Level 2（24~120h）→ Level 0（>120h 归档）
        """
        current_level = item["monitor_level"]
        if current_level == 0:
            return 0
        if item.get("is_priority"):
            return 1 if current_level != 1 else current_level

        first_seen = item.get("first_seen_at", "")
        if not first_seen:
            return current_level

        try:
            seen_dt = datetime.strptime(first_seen, "%Y-%m-%d %H:%M:%S")
            age_hours = (datetime.now() - seen_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return current_level

        t = self.config.thresholds
        if current_level == 1 and age_hours > t.level1_hours:
            logger.info(f"场景二降级: {item['item_id']} Level 1→2 ({age_hours:.1f}h)")
            return 2
        if current_level == 2 and age_hours > t.level2_hours:
            logger.info(f"场景二归档: {item['item_id']} Level 2→0 ({age_hours:.1f}h)")
            return 0

        return current_level

    # ==========================================================
    # 预测窗口管理
    # ==========================================================

    async def _check_prediction_clear(self, up: UpConfig):
        """
        检查预测窗口是否需要清空。
        如果超过 PREDICTION_IDLE_CLEAR_MINUTES 无新互动，清空 last_interacted。

        策略：读取 topic_offset.updated_at（即 set_last_interacted 的写入时间），
        判断距上次互动是否超时。不再依赖"今天是否有互动记录"——原实现是死代码，
        当天无互动时窗口永远不会被清空。
        """
        threshold = timedelta(minutes=PREDICTION_IDLE_CLEAR_MINUTES)
        for topic_id in up.topics:
            last_id = await self.db.get_last_interacted(topic_id)
            if not last_id:
                continue

            last_ts = await self.db.get_last_interacted_ts(topic_id)
            if not last_ts:
                continue
            try:
                last_time = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            elapsed = datetime.now() - last_time
            if elapsed > threshold:
                logger.info(f"[{up.name}] 预测窗口清空 (topic={topic_id}, 距上次互动 {elapsed})")
                await self.db.set_last_interacted(topic_id, "")


# ============================================================
# 快速测试入口
# ============================================================

async def _test():
    """快速验证场景二"""
    from config import Config

    cfg = Config("config.example.yaml")
    db = await Database(cfg.database.path).initialize()
    client = BiliClient()

    monitor = Scene2Monitor(db, client, cfg)

    for up in cfg.up_list:
        if not up.topics:
            print(f"[SKIP] {up.name} 未配置话题")
            continue

        print(f"\n=== 测试场景二 UP主: {up.name} ===")

        # 阶段A：发现新帖
        await monitor.discover_topic_posts(up)
        items = await db.get_items_by_level(1)
        scene2_items = [i for i in items if i.get("source") == "scene2"]
        print(f"[OK] discover: 场景二 Level1 共 {len(scene2_items)} 个")

        # 阶段B：轮询前2个帖子
        if scene2_items:
            for item in scene2_items[:2]:
                print(f"  轮询: item_id={item['item_id'][:20]}... oid={item.get('comment_oid','')}")
                await monitor._poll_item(item, up)
            print(f"[OK] poll_item 完成")

        # 互动记录
        today = await db.get_today_interactions(up.uid)
        scene2_today = [i for i in today if i.get("scene") == "scene2"]
        print(f"[OK] 今日场景二互动: {len(scene2_today)} 条")

    await db.close()
    print("\n[OK] 场景二测试通过")


if __name__ == "__main__":
    asyncio.run(_test())
