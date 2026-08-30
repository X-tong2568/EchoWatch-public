# monitor_scene4.py
"""EchoWatch 场景四：监测其他UP主动态/投稿/专栏评论区中目标UP主的评论/回复"""

import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional

from bili_client import BiliClient, SUB_COMMENT_PAGE_SIZE, parse_comment, parse_sub_comment
from config import Config, OtherUpConfig
from database import Database
from logger_config import logger

# 最大评论页数（每页约20条）
MAX_COMMENT_PAGES = 3
# 子评论检测节流间隔（秒）：同一根评论 rcount 变大后，距上次翻页 ≥ 此值才再翻
SUB_COMMENT_MIN_INTERVAL = 120
# 子评论全量扫描安全上限（页，×10条/页=最多1000条）。
# 仅用于「首次检查」和「sweep强制全量扫查」；日常窗口翻页按新增量计算，不受此限。
MAX_SUB_PAGES = 100
# 子评论基线扫查逐行请求间隔（秒）：批量 count 查询限速，防密集请求触发 B站 -412 风控
SUB_SWEEP_REQUEST_INTERVAL = 0.8


class Scene4Monitor:
    """
    场景四：监测其他UP主动态/投稿/专栏的评论区，匹配目标UP主的评论/回复。

    与其他场景的区别：
    - 匹配对象是配置的 target_uid，不是作品归属者（其他UP）
    - 不检查 up_action.like（那是作品作者的点赞，不是目标UP主的互动）
    - 不监测置顶/priority（其他UP的置顶与目标UP主回复无关）
    - 无即时单发，互动由调度循环批量合并通知（同场景二/三）

    两阶段轮询（同场景三）：
    - 阶段A：拉取其他UP空间动态列表 → 新帖子入库（source="scene4"）
    - 阶段B：轮询监测队列中各帖子的评论区，匹配目标UP主的评论/回复

    分级：L1（0~24h）→ L2（24~120h）→ L0（归档），与场景一相同阈值。
    子评论：rcount 基线节流（有评论数量字段 → 场景2式节流）。
    截图：v2.0 起不再入库截图——需要原帖图时在推送前按需补截，超时/超限邮件内空着。
    """

    def __init__(self, db: Database, client: BiliClient, config: Config, screenshotter=None):
        self.db = db
        self.client = client
        self.config = config
        self.screenshotter = screenshotter  # 推送前按需补截用（v2.0 起）
        self.target_uid = config.scene4.target_uid  # 目标UP主UID
        self._polling_l1 = False
        self._polling_l2 = False
        self._disabled_until: dict = {}  # item_id → 跳过轮询截止时间（评论已关闭的帖子）

    # ==========================================================
    # 阶段A：发现其他UP新动态
    # ==========================================================

    async def discover(self, other_up: OtherUpConfig):
        """
        拉取其他UP空间动态列表（含动态/投稿/专栏），发现新帖子并加入监测队列。

        策略（同场景三发现）：
        - 全部入库（已存在的跳过），pub_ts 作为 first_seen_at → 旧帖自动落 L2/L0
        - v2.0 起不截原帖图（入库仅数据），推送时按需补截
        - 跨场景去重：comment_oid 已被其他场景占用则跳过（场景一不受限，
          联合投稿场景由场景一先手，此处自然跳过，不会双份监测）

        Args:
            other_up: 其他UP配置（uid、name）
        """
        try:
            dynamics = await self.client.get_user_dynamics(other_up.uid)
        except Exception as e:
            logger.error(f"获取其他UP动态列表失败 (UP={other_up.name}): {e}")
            return

        new_count = 0
        for dyn in dynamics:
            dyn_id = dyn.get("dynamic_id", "")
            comment_oid = dyn.get("comment_oid", "")
            dyn_type = dyn.get("type", "")
            pub_ts = dyn.get("pub_ts", 0)

            if not dyn_id or not comment_oid:
                continue

            # 跨场景去重：同一评论区已被其他场景占用则跳过
            # （与场景三同规则，防双份监测与场景归属混乱；场景一不受限）
            existing = await self.db.get_item_by_comment_oid(comment_oid)
            if existing and existing["item_id"] != dyn_id:
                logger.debug(
                    f"评论区已被监测 (oid={comment_oid} item={existing['item_id']})，跳过 {dyn_id}"
                )
                continue

            inserted = await self.db.upsert_item(
                item_id=dyn_id,
                comment_oid=comment_oid,
                item_type=dyn_type,
                source="scene4",
                up_uid=other_up.uid,
                pub_ts=pub_ts,
            )
            if inserted:
                new_count += 1
                # v2.0：不再入库即截图——截图改为推送时按需补截（有目标互动的帖才截），
                # 发现层绝大多数动态不会被推送，白截浪费机场流量（2026-08-28 流量事故）
            else:
                # 已存在但oid可能不对，尝试修正（动态ID → 真实aid）
                await self.db.update_comment_oid(dyn_id, comment_oid)

        if new_count > 0:
            logger.info(f"[{other_up.name}] 场景四发现 {new_count} 个新帖子")

    # ==========================================================
    # 阶段B：轮询帖子评论区
    # ==========================================================

    async def poll_all(self, other_up: OtherUpConfig):
        """
        轮询所有 Level 1 帖子的评论区（高频）。
        加防重入锁：上一轮没跑完则跳过，避免重叠。
        """
        if self._polling_l1:
            logger.debug(f"[{other_up.name}] 场景四L1轮询跳过（上一轮未完成）")
            return
        self._polling_l1 = True
        try:
            level1_items = await self.db.get_items_by_level(1, source="scene4")
            # 只轮询本其他UP的帖子（多UP时互不干扰）
            items = [it for it in level1_items if it.get("up_uid") == other_up.uid]

            logger.debug(f"[{other_up.name}] 场景四轮询: Level1={len(items)}")

            for item in items:
                await asyncio.sleep(random.uniform(
                    self.config.intervals.random_delay_min,
                    self.config.intervals.random_delay_max,
                ))
                try:
                    await self._poll_item(item, other_up)
                except Exception as e:
                    logger.error(f"场景四轮询失败 item={item['item_id']}: {e}")
        finally:
            self._polling_l1 = False

    async def poll_level2(self, other_up: OtherUpConfig):
        """低频轮询 Level 2 帖子（24~120h 的旧帖）"""
        if self._polling_l2:
            logger.debug(f"[{other_up.name}] 场景四L2轮询跳过（上一轮未完成）")
            return
        self._polling_l2 = True
        try:
            level2_items = await self.db.get_items_by_level(2, source="scene4")
            items = [it for it in level2_items if it.get("up_uid") == other_up.uid]
            if not items:
                return

            logger.debug(f"[{other_up.name}] 场景四L2轮询: {len(items)} 个")

            for item in items:
                await asyncio.sleep(random.uniform(
                    self.config.intervals.random_delay_min,
                    self.config.intervals.random_delay_max,
                ))
                try:
                    await self._poll_item(item, other_up)
                except Exception as e:
                    logger.error(f"场景四L2轮询失败 item={item['item_id']}: {e}")
        finally:
            self._polling_l2 = False

    async def recheck_all_levels(self, other_up: OtherUpConfig):
        """
        定期重新分级：遍历所有活跃的 scene4 监测项，按 first_seen_at 重新算级别。
        确保不依赖轮询也能正常 L1→L2→L0 流转。
        """
        items = await self.db.get_active_items_by_source("scene4")
        changed = 0
        for item in items:
            new_level = self._check_level_transition(item)
            if new_level != item["monitor_level"]:
                await self.db.set_level(item["item_id"], new_level)
                changed += 1
        if changed > 0:
            logger.info(f"[{other_up.name}] 场景四重新分级: {changed} 条变更")

    def _check_level_transition(self, item: dict) -> int:
        """
        根据时间阈值判断监测级别（与场景一同逻辑）。
        - Level 1 （0~24h）→ Level 2（24~120h）→ Level 0（>120h 归档）
        """
        current_level = item["monitor_level"]
        if current_level == 0:
            return 0

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
            logger.info(f"场景四降级: {item['item_id']} Level 1→2 ({age_hours:.1f}h)")
            return 2
        if current_level == 2 and age_hours > t.level2_hours:
            logger.info(f"场景四归档: {item['item_id']} Level 2→0 ({age_hours:.1f}h)")
            return 0

        return current_level

    # ==========================================================
    # 轮询单个帖子的评论区
    # ==========================================================

    async def _poll_item(self, item: dict, other_up: OtherUpConfig):
        """
        轮询单个帖子的评论区（匹配 mid == target_uid）。

        Args:
            item: 数据库中的 monitored_items 行
            other_up: 其他UP配置
        """
        item_id = item["item_id"]
        comment_oid = item.get("comment_oid", "")
        dyn_type = item["item_type"]

        # 评论已关闭的帖子：跳过轮询（1小时后再试，避免无效请求风暴）
        until = self._disabled_until.get(item_id)
        if until and datetime.now() < until:
            return

        comment_type = self.client.get_comment_type(dyn_type)

        # 级别升降
        new_level = self._check_level_transition(item)
        if new_level != item["monitor_level"]:
            await self.db.set_level(item_id, new_level)
            # 级别变更后本轮结束，下一轮按新级别处理：
            # 防止刚入库的历史帖降级时顺带扫评论区，把历史评论当新互动
            return

        try:
            oid = int(comment_oid)
        except (ValueError, TypeError):
            logger.error(f"无效的 comment_oid: {comment_oid} (item={item_id})")
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
                logger.warning(f"场景四评论API失败 page={page} oid={oid}: {e}")
                break

            # 评论功能已关闭/无评论（确定性错误）：标记跳过1小时
            if resp and resp.get("disabled"):
                self._disabled_until[item_id] = datetime.now() + timedelta(hours=1)
                break

            replies = resp.get("replies", []) if resp else []
            top_replies = resp.get("top_replies", []) if resp else []

            # 置顶评论（仅第一页有，需单独处理）
            for raw in top_replies:
                await self._process_comment(raw, oid, comment_type, item_id)

            if not replies:
                break

            for raw in replies:
                await self._process_comment(raw, oid, comment_type, item_id)

            # 游标翻页：优先看 is_end，其次看 pagination_reply.next_offset
            cursor = resp.get("cursor", {}) if resp else {}
            if cursor.get("is_end"):
                break
            pag_reply = cursor.get("pagination_reply", {}) if isinstance(cursor, dict) else {}
            pagination_str = pag_reply.get("next_offset", "")
            if not pagination_str or pagination_str == "no-next-offset":
                break

        await self.db.update_poll_time(item_id)

    async def _process_comment(self, raw: dict, oid: int, comment_type, item_id: str) -> bool:
        """
        处理单条一级评论：仅匹配 target_uid自己的评论。
        不检查 up_action.like（那是作品作者的点赞，语义同场景3）。

        Args:
            raw: API 返回的原始评论数据
            oid: 评论区的 OID
            comment_type: 评论区类型枚举
            item_id: 所属帖子ID

        Returns:
            True 表示目标UP主在此评论处有互动（用于统计）
        """
        parsed = parse_comment(raw)
        rpid = parsed["rpid"]
        mid = parsed["mid"]
        content = parsed["content"]
        replies_count = parsed["replies_count"]

        target_found = False

        # 匹配目标UP主的评论
        if mid == self.target_uid:
            target_found = True
            await self.db.insert_interaction({
                "up_uid": self.target_uid,
                "item_id": item_id,
                "comment_id": str(rpid),
                "is_sub_reply": False,
                "parent_content": None,
                "parent_author": None,
                "content": content,
                "rich_content": parsed.get("rich_content", ""),
                "up_liked": False,
                "scene": "scene4",
            })

        # 子评论（rcount 基线节流：首次见立即翻；rcount 变大 且 距上次翻 ≥ 2min 才翻）
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
                    oid, comment_type, rpid, item_id, root_context,
                    replies_count=replies_count,
                )
                if sub_found:
                    target_found = True

        return target_found

    async def _process_sub_comments(self, oid: int, comment_type,
                                     root_rpid, item_id: str,
                                     root_context: dict, replies_count: int = 0,
                                     force_full: bool = False) -> bool:
        """
        拉取子评论，匹配目标UP主的回复（尾部窗口翻页，同场景一 v1.6.0）。

        翻页策略（楼中楼按时间升序，新回复总在尾部）：
        - 日常触发：窗口 = [基线位置-2页, 最新末页]，只翻新增部分
        - 首次检查（基线缺失）/ force_full（sweep强制扫查）：从第1页全量翻到真实末页
        - 窗口没翻完（中途异常/空页截断）→ 不打基线，下一轮重翻（防永久漏检）

        成功翻完窗口才更新检测基线（rcount 节流用）。
        获取被回复评论上下文：拉取窗口内所有子评论构建查找表还原 parent。

        Args:
            force_full: 强制全量扫描（sweep 兜底扫查用）

        Returns:
            True 表示目标UP主在此处有互动
        """
        SUB_PAGE = SUB_COMMENT_PAGE_SIZE

        # 读取基线，确定翻页窗口
        baseline = await self.db.get_sub_baseline(item_id, root_rpid)
        prev = int(baseline["last_rcount"]) if baseline else 0
        total = int(replies_count or 0)

        if force_full or baseline is None or prev == 0:
            # 全量扫描：首次检查 / sweep 强制扫查，翻到真实末页为止
            start_page = 1
            end_page = min(MAX_SUB_PAGES, max(1, (total + SUB_PAGE - 1) // SUB_PAGE))
        else:
            new_cnt = total - prev
            if new_cnt <= 0:
                # 无新增回复（正常状态：基线未变），无需翻页
                return True
            # 新回复在尾部；粉丝删除评论会使位置前移，多翻2页兜底
            start_page = max(1, prev // SUB_PAGE - 2)
            # 真实末页不截断（窗口宽度才受 MAX_SUB_PAGES 限制，与 total 绝对值无关）
            end_page = (total + SUB_PAGE - 1) // SUB_PAGE
            # 窗口宽度上限（2026-08-29 三方核查修正）：限制的是宽度而非 end_page——
            # 子评论按时间升序、新回复在高页码，若回退 start_page=1 会翻最老条目且
            # 虚标基线导致稳定漏检；正确做法是砍头部（老数据）保尾部（新增区）
            if end_page - start_page + 1 > MAX_SUB_PAGES:
                start_page = end_page - MAX_SUB_PAGES + 1

        all_subs = []  # 收集窗口内所有子评论
        completed = False  # 是否翻完预期窗口（未翻完不打基线，下轮重试）
        for page in range(start_page, end_page + 1):
            try:
                resp = await self.client.get_sub_comments(
                    oid=oid, comment_type=comment_type,
                    root_rpid=root_rpid, page_index=page,
                )
            except Exception as e:
                logger.debug(f"场景四子评论API失败 root={root_rpid} page={page}: {e}")
                break  # completed 保持 False → 不打基线

            if resp and resp.get("disabled"):
                # 评论区已关闭（确定性错误）：不打基线，避免反复空翻
                return False
            if resp and resp.get("banned"):
                # -412 风控：本轮跳过，不打基线，下轮重试（防误报）
                logger.warning(f"场景四子评论风控跳过 root={root_rpid} page={page}")
                break

            subs = resp.get("replies") or [] if resp else []
            all_subs.extend(subs)

            # 正常收尾：空页 / 不满一页（防 count 虚高）/ 翻到窗口末尾
            if not subs or len(subs) < SUB_PAGE or page >= end_page:
                completed = True
                break

        if not all_subs:
            return False

        if not completed:
            # 窗口没翻完 → 不打基线，下一轮 rcount 仍大于基线时重翻（防永久漏检）
            logger.warning(
                f"场景四子评论窗口未翻完 root={root_rpid} 已取{len(all_subs)}条 目标={total}，本轮不更新基线"
            )
            return False

        # 成功翻完窗口 → 更新检测基线（只覆盖当前值，不保留历史）
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.db.upsert_sub_baseline(item_id, root_rpid, replies_count, now_str)

        # 构建子评论查找表
        sub_lookup = {}
        for raw in all_subs:
            parsed = parse_sub_comment(raw)
            sub_lookup[parsed["rpid"]] = {
                "uname": parsed["uname"],
                "content": parsed["content"],
                "rich_content": parsed.get("rich_content", ""),
            }

        target_found = False
        for raw in all_subs:
            parsed = parse_sub_comment(raw)
            # 仅匹配目标UP主的回复
            if parsed["mid"] != self.target_uid:
                continue

            target_found = True
            parent_rpid = parsed["parent_rpid"]
            if parent_rpid == root_rpid:
                # root_context 可能为 None（sweep 兜底扫查场景，无主列表上下文）
                parent_content = root_context["content"] if root_context else ""
                parent_author = root_context["uname"] if root_context else ""
                parent_rich = root_context.get("rich_content", "") if root_context else ""
            elif parent_rpid in sub_lookup:
                parent_content = sub_lookup[parent_rpid]["content"]
                parent_author = sub_lookup[parent_rpid]["uname"]
                parent_rich = sub_lookup[parent_rpid]["rich_content"]
            else:
                parent_content = ""
                parent_author = ""
                parent_rich = ""

            await self.db.insert_interaction({
                "up_uid": self.target_uid,
                "item_id": item_id,
                "comment_id": str(parsed["rpid"]),
                "is_sub_reply": True,
                "parent_content": parent_content,
                "parent_author": parent_author,
                "parent_rich_content": parent_rich,
                "content": parsed["content"],
                "rich_content": parsed.get("rich_content", ""),
                "up_liked": False,
                "scene": "scene4",
            })

        return target_found

    # ==========================================================
    # 子评论基线兜底扫查（同场景一 v1.5.0）
    # ==========================================================

    async def sweep_sub_comment_baselines(self):
        """
        子评论基线兜底扫查：不依赖主评论列表可见性。

        直接对 sub_comment_baseline 表里 scene4 的根评论调子评论 API 查权威 count，
        count 变大时触发增量翻页检测（防静默漏检）。

        2026-08-29 审计改造（风暴镇压，同场景一）：
        - 已归档（L0）基线在 SQL 层过滤（get_all_sub_baselines JOIN）
        - count 无变化但基线过期（>sub_sweep_max_age）时仅同步权威 count，
          零翻页（原实现此时照样 force_full 全量重翻，是 -412 风暴主因）
        无主列表上下文时 root_context 传 None，parent 显示留空。
        """
        other_uids = {other.uid for other in self.config.scene4.other_up_list}
        rows = await self.db.get_all_sub_baselines(source="scene4", up_uids=list(other_uids))
        if not rows:
            return

        checked = 0   # 实际翻页检测条数
        synced = 0    # 仅同步权威 count 的条数（零翻页）
        for row in rows:
            try:
                oid = int(row.get("comment_oid") or 0)
            except (ValueError, TypeError):
                continue
            if not oid:
                continue
            comment_type = self.client.get_comment_type(row.get("item_type", ""))
            root_rpid = int(row["root_rpid"])
            # 查第1页拿权威 count（每基线1次请求，节流）
            try:
                resp = await self.client.get_sub_comments(oid, comment_type, root_rpid, 1)
            except Exception as e:
                logger.debug(f"场景四子评论基线扫查失败 item={row['item_id']} root={root_rpid}: {e}")
                # 失败也占请求配额，同样限速（防密集请求触发风控）
                await asyncio.sleep(SUB_SWEEP_REQUEST_INTERVAL)
                continue
            # 限速：每基线1次 count 请求后等待，降低批量扫查的请求密度
            await asyncio.sleep(SUB_SWEEP_REQUEST_INTERVAL)
            count = (resp.get("page") or {}).get("count", 0)
            if not count:
                continue
            # 距上次翻页的时间
            gap_sec = float("inf")
            try:
                last_dt = datetime.strptime(row["last_check_ts"], "%Y-%m-%d %H:%M:%S")
                gap_sec = (datetime.now() - last_dt).total_seconds()
            except (ValueError, TypeError):
                pass
            if count > row["last_rcount"] and gap_sec >= SUB_COMMENT_MIN_INTERVAL:
                # 有新增回复 → 增量窗口翻页（不再全量重翻）
                checked += 1
                logger.info(
                    f"场景四子评论基线扫查触发: item={row['item_id']} root={root_rpid} "
                    f"count={count} 基线={row['last_rcount']} 距今={int(gap_sec)}s"
                )
                await self._process_sub_comments(
                    oid, comment_type, root_rpid, row["item_id"],
                    root_context=None, replies_count=count,
                )
            elif gap_sec >= self.config.intervals.sub_sweep_max_age:
                # 基线过期但 count 未变：权威 count 同步基线，零翻页
                synced += 1
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                await self.db.upsert_sub_baseline(row["item_id"], root_rpid, count, now_str)

        logger.info(
            f"场景四子评论基线扫查: 基线{len(rows)}条 翻页{checked}条 过期同步{synced}条"
        )


# ============================================================
# 快速测试入口
# ============================================================

async def _test():
    """快速验证场景四"""
    from config import Config

    cfg = Config("config.yaml")
    db = await Database(cfg.database.path).initialize()
    client = BiliClient()

    monitor = Scene4Monitor(db, client, cfg)

    if not cfg.scene4.other_up_list:
        print("[SKIP] 未配置其他UP")
        return

    for other_up in cfg.scene4.other_up_list:
        print(f"\n=== 测试其他UP: {other_up.name} ===")

        # 阶段A：发现新帖子
        await monitor.discover(other_up)
        items = await db.get_items_by_level(1, source="scene4")
        scene4_items = [i for i in items if i.get("up_uid") == other_up.uid]
        print(f"[OK] discover: scene4 Level1 共 {len(scene4_items)} 个")

        # 阶段B：轮询前2个帖子（L1 不足时用 L2 补充）
        l2 = await db.get_items_by_level(2, source="scene4")
        candidates = [i for i in (l2 + items) if i.get("up_uid") == other_up.uid][:2]
        for item in candidates:
            print(f"  轮询: item_id={item['item_id'][:20]}... oid={item.get('comment_oid','')}")
            await monitor._poll_item(item, other_up)
        print(f"[OK] poll_item 完成")

        # 互动记录
        today = await db.get_today_interactions(monitor.target_uid)
        scene4_today = [i for i in today if i.get("scene") == "scene4"]
        print(f"[OK] 今日场景四互动: {len(scene4_today)} 条")

    await db.close()
    print("\n[OK] 场景四测试通过")


if __name__ == "__main__":
    asyncio.run(_test())
