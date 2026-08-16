# monitor_scene1.py
"""EchoWatch 场景一：监测 UP主 自身作品的评论区互动"""

import asyncio
import random
from datetime import datetime

from bili_client import BiliClient, SUB_COMMENT_PAGE_SIZE, parse_comment, parse_sub_comment
from config import Config, UpConfig
from database import Database
from email_notifier import Notifier
from logger_config import logger
from pinned_dynamic_monitor import get_current_pinned_id


# 最大评论页数（每页约20条）
MAX_COMMENT_PAGES = 3
# 子评论检测节流间隔（秒）：同一根评论 rcount 变大后，距上次翻页 ≥ 此值才再翻
SUB_COMMENT_MIN_INTERVAL = 120
# 子评论全量扫描安全上限（页，×10条/页=最多1000条）。
# 仅用于「首次检查」和「sweep强制全量扫查」；日常窗口翻页按新增量计算，不受此限。
MAX_SUB_PAGES = 100


# ============================================================
# Scene1Monitor 类
# ============================================================

class Scene1Monitor:
    """
    场景一：监测 UP主 自身作品评论区。

    监测目标：
    - UP主 亲自发的评论/回复（mid == up_uid）
    - 被标注"UP觉得很赞"的评论（up_action.like == True）

    priority 机制：
    - is_priority=True 的作品始终以 Level 1 轮询，不受时间降级影响
    - 用于监测置顶动态等关键作品
    """

    def __init__(self, db: Database, client: BiliClient, config: Config, notifier: Notifier = None):
        self.db = db
        self.client = client
        self.config = config
        self.notifier = notifier
        self._polling_l1 = False
        self._polling_l2 = False
        self._polling_priority = False

    async def poll_priority_only(self, up: UpConfig):
        """仅轮询 priority 项（高频，无随机延迟）。
        加防重入锁：get_comments 失败重试 50s×3 期间，
        新循环（1~5s 一轮）不会并发堆积请求 → 避免请求风暴加剧风控。
        """
        if self._polling_priority:
            logger.debug(f"[{up.name}] priority轮询跳过（上一轮未完成）")
            return
        self._polling_priority = True
        try:
            items = await self.db.get_priority_items()
            if not items:
                return
            for item in items:
                try:
                    await self._poll_item(item, up)
                except Exception as e:
                    logger.error(f"priority轮询失败 item={item['item_id']}: {e}")
        finally:
            self._polling_priority = False

    # ==========================================================
    # 发现新作品
    # ==========================================================

    async def discover(self, up: UpConfig):
        """
        发现新作品并加入监测队列。

        步骤：
        1. 同步 priority 动态（自动发现置顶 + detail API 获取 oid，不依赖发现层）
        2. 拉取空间动态列表发现新作品（可能因风控失败，不影响 priority）

        Args:
            up: UP主 配置（含 uid、name、priority_dynamics）
        """
        # 步骤1：确保 priority 动态始终在监测队列中（绕过发现层）
        await self._sync_priority_dynamics(up)

        # 步骤2：常规发现（空间动态 / 视频搜索降级）
        try:
            dynamics = await self.client.get_user_dynamics(up.uid)
        except Exception as e:
            logger.error(f"获取动态列表失败 (UP={up.name}): {e}")
            return

        new_count = 0
        for dyn in dynamics:
            dyn_id = dyn.get("dynamic_id", "")
            comment_oid = dyn.get("comment_oid", "")
            dyn_type = dyn.get("type", "")
            pub_ts = dyn.get("pub_ts", 0)

            if not dyn_id or not comment_oid:
                continue

            is_priority = dyn_id in up.priority_dynamics

            inserted = await self.db.upsert_item(
                item_id=dyn_id,
                comment_oid=comment_oid,
                item_type=dyn_type,
                source="scene1",
                up_uid=up.uid,
                is_priority=is_priority,
                pub_ts=pub_ts,
            )
            if inserted:
                new_count += 1
            else:
                # 已存在但oid可能不对，尝试修正（动态ID → 真实aid）
                await self.db.update_comment_oid(dyn_id, comment_oid)

        if new_count > 0:
            logger.info(f"[{up.name}] 发现 {new_count} 个新作品")
        else:
            logger.debug(f"[{up.name}] 本轮无新作品")

    async def _sync_priority_dynamics(self, up: UpConfig):
        """
        自动发现 + 兜底：优先用 API 发现的置顶动态ID，失败则用 config 列表兜底。

        1. 先从 pinned_dynamic_state.json 读取自动发现的置顶ID
        2. 如果自动发现失败，降级使用 config 中的 priority_dynamics 列表
        3. 通过 detail API 获取 oid+type 并入库，标记 is_priority=True
        """
        # 获取应监控的 priority 动态ID列表（自动发现优先，config兜底）
        auto_id = get_current_pinned_id()
        if auto_id:
            priority_ids = [auto_id]
            logger.debug(f"[{up.name}] 使用自动发现的置顶ID: {auto_id}")
        elif up.priority_dynamics:
            priority_ids = up.priority_dynamics
            logger.debug(f"[{up.name}] 自动发现为空，使用config兜底: {priority_ids}")
        else:
            # 无置顶 ID 可用（自动发现为空且 config 未配置）：
            # 不清除 DB 中的旧 priority 标记 —— 只有检测到新的置顶ID才能替换旧的，
            # 绝不允许"空白替换旧的"（否则 feed 间歇性漏检会误清全部 priority 标记）
            logger.warning(
                f"[{up.name}] 无置顶动态ID（自动发现为空且config未配置），"
                f"保留现有 priority 标记，等待新置顶出现"
            )
            return

        # 清理已不在列表中的旧 priority 标记
        await self.db.clear_stale_priority(priority_ids)

        # 被替换下来的旧 priority 项需要立即重新分级（否则会残留L1直到下次recheck）
        await self.recheck_all_levels(up)

        for dyn_id in priority_ids:
            try:
                detail = await self.client._signed_get(
                    "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail",
                    {"id": dyn_id},
                )
                item = detail.get("item") or {}
                basic = item.get("basic", {})
                dyn_type = item.get("type", "DYNAMIC_TYPE_DRAW")

                # 获取评论区 oid：视频类用 aid，其他用 comment_id_str
                comment_oid = ""
                modules = item.get("modules", {})
                mod_dyn = modules.get("module_dynamic") or {}
                major = mod_dyn.get("major") or {}
                archive = major.get("archive") or {}
                aid = str(archive.get("aid", ""))
                if dyn_type == "DYNAMIC_TYPE_AV" and aid:
                    comment_oid = aid
                else:
                    comment_oid = str(basic.get("comment_id_str", ""))
                comment_type = basic.get("comment_type", 11)

                if not comment_oid:
                    logger.warning(f"priority 动态 {dyn_id} 无 comment_id_str")
                    continue

                # 发布时间（用于分级）
                modules = item.get("modules", {})
                mod_author = modules.get("module_author") or {}
                pub_ts = mod_author.get("pub_ts", 0)

                inserted = await self.db.upsert_item(
                    item_id=dyn_id,
                    comment_oid=comment_oid,
                    item_type=dyn_type,
                    source="scene1",
                    up_uid=up.uid,
                    is_priority=True,
                    pub_ts=pub_ts,
                )
                if inserted:
                    logger.info(f"[{up.name}] priority 动态已入库: {dyn_id} (oid={comment_oid})")
                else:
                    # 已存在但可能未标记 priority，确保标记
                    await self.db.set_priority(dyn_id)
                    logger.debug(f"[{up.name}] priority 动态已存在: {dyn_id}")

            except Exception as e:
                logger.error(f"[{up.name}] priority 动态同步失败 {dyn_id}: {e}")

    # ==========================================================
    # 轮询评论
    # ==========================================================

    async def poll_all(self, up: UpConfig):
        """
        轮询所有活跃监测项的评论区。

        流程：
        1. 获取 Level 1 项 + priority 项
        2. 逐个检查时间阈值，执行级别升降
        3. 拉取评论 → 匹配 UP主/点赞 → 写库
        """
        # 加锁：上一轮没跑完则跳过，避免重叠
        if self._polling_l1:
            logger.debug(f"[{up.name}] 场景一L1轮询跳过（上一轮未完成）")
            return
        self._polling_l1 = True
        try:
            await self._poll_all_impl(up)
        finally:
            self._polling_l1 = False

    async def _poll_all_impl(self, up: UpConfig):
        # Level 1 项（排除 priority 项，避免与 poll_priority_only 重复轮询→重复发邮件）
        level1_items = await self.db.get_items_by_level(1, source="scene1")

        # 过滤掉 is_priority 项（由 poll_priority_only 高频覆盖）
        items = [it for it in level1_items if not it.get("is_priority")]

        logger.debug(f"[{up.name}] 场景一轮询: Level1={len(level1_items)}, 排除priority后={len(items)}")

        for item in items:
            # 请求间随机延迟
            await asyncio.sleep(random.uniform(
                self.config.intervals.random_delay_min,
                self.config.intervals.random_delay_max,
            ))

            try:
                await self._poll_item(item, up)
            except Exception as e:
                logger.error(f"轮询评论失败 item={item['item_id']}: {e}")

    async def _poll_item(self, item: dict, up: UpConfig):
        """
        轮询单个监测项的评论区。

        Args:
            item: 数据库中的 monitored_items 行
            up: UP主 配置
        """
        item_id = item["item_id"]
        comment_oid = item.get("comment_oid", "")
        dyn_type = item["item_type"]

        # 确定评论类型（使用客户端的统一映射）
        comment_type = self.client.get_comment_type(dyn_type)

        # 检查时间阈值，执行级别升降
        new_level = self._check_level_transition(item)
        if new_level != item["monitor_level"]:
            await self.db.set_level(item_id, new_level)
            if new_level == 0:
                return  # 已归档，跳过本轮

        # 尝试解析 OID
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
                logger.warning(f"评论API失败 page={page} oid={oid}: {e}")
                break

            # 评论功能已关闭/无评论（确定性错误）：本轮直接结束
            if resp and resp.get("disabled"):
                break

            replies = resp.get("replies", []) if resp else []
            top_replies = resp.get("top_replies", []) if resp else []

            # 置顶评论（仅第一页有，需单独处理）
            for raw in top_replies:
                await self._process_comment(raw, oid, comment_type, item_id, up)

            if not replies:
                break

            for raw in replies:
                await self._process_comment(raw, oid, comment_type, item_id, up)

            # 游标翻页：优先看 is_end，其次看 pagination_reply.next_offset
            cursor = resp.get("cursor", {}) if resp else {}
            if cursor.get("is_end"):
                break
            pag_reply = cursor.get("pagination_reply", {}) if isinstance(cursor, dict) else {}
            pagination_str = pag_reply.get("next_offset", "")
            if not pagination_str or pagination_str == "no-next-offset":
                break

        # priority 项：主评论即时推送邮件（不走30s队列）；
        # 子评论留给 _priority_sub_batch_loop 每几分钟合并一封（像场景二），日报照常汇总
        if self.notifier and item.get("is_priority"):
            interactions = await self.db.get_unnotified_immediate(scene="scene1")
            for interaction in interactions:
                if interaction["item_id"] != item_id:
                    continue
                if interaction.get("is_sub_reply"):
                    # 子评论：留给批量汇总循环，这里不即时发也不标记
                    continue
                await self.notifier.send_immediate(
                    interaction, up.name, item.get("item_type", ""),
                    is_priority=True,
                )

        # 更新最后轮询时间
        await self.db.update_poll_time(item_id)

    async def _process_comment(self, raw: dict, oid: int,
                                comment_type, item_id: str, up: UpConfig):
        """
        处理单条一级评论：匹配 UP主 自己的评论 + "UP觉得很赞"。

        Args:
            raw: API 返回的原始评论数据
            oid: 评论区的 OID
            comment_type: 评论区类型枚举
            item_id: 所属作品ID
            up: UP主 配置
        """
        parsed = parse_comment(raw)
        rpid = parsed["rpid"]
        mid = parsed["mid"]
        content = parsed["content"]
        up_liked = parsed["up_action_like"]
        replies_count = parsed["replies_count"]

        # 检查 1：UP主 自己的评论
        if mid == up.uid:
            await self.db.insert_interaction({
                "up_uid": up.uid,
                "item_id": item_id,
                "comment_id": str(rpid),
                "is_sub_reply": False,
                "parent_content": None,
                "parent_author": None,
                "content": content,
                "rich_content": parsed.get("rich_content", ""),
                "up_liked": False,  # UP主自己的评论不标记up_liked
                "scene": "scene1",
            })

        # 检查 2：UP觉得很赞（不重复记录 UP主 自己的评论）
        if up_liked and mid != up.uid:
            await self.db.insert_interaction({
                "up_uid": up.uid,
                "item_id": item_id,
                "comment_id": f"{rpid}_like",  # 与 UP主自己的评论区分
                "is_sub_reply": False,
                "parent_content": None,
                "parent_author": None,
                "content": content,
                "rich_content": parsed.get("rich_content", ""),
                "up_liked": True,
                "scene": "scene1",
            })

        # 检查 3：子评论（楼中楼）—— 基线节流
        # 触发条件：基线无记录（首次见）→ 立即翻；有记录 → rcount 变大 且 距上次翻 ≥ 2min
        # 平时每轮只做 1 次 SELECT 判断，避免对同一楼反复翻页打爆 API
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
                # 传递根评论的上下文（用于子评论的 parent 查找）
                root_context = {
                    "rpid": rpid,
                    "uname": parsed["uname"],
                    "content": content,
                    "rich_content": parsed.get("rich_content", ""),
                }
                await self._process_sub_comments(
                    oid, comment_type, rpid, item_id, up, root_context,
                    replies_count=replies_count,
                )

    async def _process_sub_comments(self, oid: int, comment_type,
                                     root_rpid, item_id: str, up: UpConfig,
                                     root_context: dict, replies_count: int = 0,
                                     check_up_liked: bool = True,
                                     force_full: bool = False):
        """
        拉取并处理子评论，匹配 UP主 的回复（尾部窗口翻页，v1.6.0）。

        翻页策略（楼中楼按时间升序，新回复总在尾部）：
        - 日常触发：窗口 = [基线位置-2页, 最新末页]，只翻新增部分；
          大量涌入场景窗口随新增量自动扩张，不受 MAX_SUB_PAGES 限制。
        - 首次检查（基线缺失）/ force_full（sweep强制扫查）：从第1页
          全量翻到真实末页（上限 MAX_SUB_PAGES 页），兜底恢复误标基线。
        - 窗口没翻完（中途异常/空页截断）→ 不打基线，下一轮重翻整个
          窗口（防"收集不全却误标基线"的永久漏检；重复翻到靠comment_id去重）。

        成功翻完窗口才更新检测基线（rcount 节流用）。

        获取被回复评论上下文的策略：
        1. 拉取窗口内所有子评论，构建 {rpid → {uname, content}} 查找表
        2. 对每条 UP主 子评论，parent_rpid 指向被回复者：
           - parent_rpid == root_rpid → 被回复的是根评论（root_context）
           - parent_rpid 在子评论查找表中 → 被回复的是其他子评论
           - 否则 → 上下文未知（窗口外的老评论，留空）

        Args:
            check_up_liked: 是否检测"UP觉得很赞"（up_action.like）。
                注意：该字段语义是"评论区作者点赞"。场景2 话题下存在粉丝发帖，
                粉丝帖的评论区作者是粉丝，up_action.like 表示发帖粉丝点赞，
                不是UP主点赞 → 场景2 item 必须传 False（与 scene2 已删的
                up_liked 功能一致），否则会把粉丝点赞误报成UP主互动。
            force_full: 强制全量扫描（sweep 兜底扫查用，防截断误标基线）。
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
            end_page = (total + SUB_PAGE - 1) // SUB_PAGE

        all_subs = []  # 收集窗口内所有子评论
        completed = False  # 是否翻完预期窗口（未翻完不打基线，下轮重试）
        for page in range(start_page, end_page + 1):
            try:
                resp = await self.client.get_sub_comments(
                    oid=oid, comment_type=comment_type,
                    root_rpid=root_rpid, page_index=page,
                )
            except Exception as e:
                logger.debug(f"子评论API失败 root={root_rpid} page={page}: {e}")
                break  # completed 保持 False → 不打基线

            if resp and resp.get("disabled"):
                # 评论区已关闭（确定性错误）：不打基线，避免反复空翻
                return False

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
                f"子评论窗口未翻完 root={root_rpid} 已取{len(all_subs)}条 目标={total}，本轮不更新基线"
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

        for raw in all_subs:
            parsed = parse_sub_comment(raw)

            # 检查 1：UP主 自己的子回复
            if parsed["mid"] == up.uid:
                parent_rpid = parsed["parent_rpid"]
                if parent_rpid == root_rpid:
                    # root_context 可能为 None（兜底扫查场景，无主列表上下文）
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
                    "scene": "scene1",
                })

            # 检查 2：子评论被 UP 觉得很赞（不重复记录 UP主 自己的回复）
            # 仅 scene1（UP主自己的作品）启用；scene2 含粉丝帖，up_action.like 是发帖人点赞
            if check_up_liked and parsed["up_action_like"] and parsed["mid"] != up.uid:
                await self.db.insert_interaction({
                    "up_uid": up.uid,
                    "item_id": item_id,
                    "comment_id": f"{parsed['rpid']}_like",
                    "is_sub_reply": True,
                    "parent_content": "",
                    "parent_author": "",
                    "content": parsed["content"],
                    "rich_content": parsed.get("rich_content", ""),
                    "up_liked": True,
                    "scene": "scene1",
                })

        return True  # 成功拿到子评论数据

    async def sweep_sub_comment_baselines(self):
        """
        子评论基线兜底扫查（v1.5.0）：不依赖主评论列表可见性。

        背景：主评论翻页游标格式错误导致 page≥1 全 -400，根评论滑出
        可见窗口后 check 3 永不触发，子评论静默漏检。此扫查直接对
        sub_comment_baseline 表里每条根评论调子评论 API 查权威 count：
        - count > 基线 且 距上次翻 ≥ 2min → 完整翻页检测
        - 基线超过 sub_sweep_max_age（默认2h）→ 强制重新翻页（防 count 不同步）

        无主列表上下文时 root_context 传 None，parent 显示留空。
        """
        rows = await self.db.get_all_sub_baselines()
        if not rows:
            return
        logger.debug(f"子评论基线扫查: {len(rows)} 条基线")
        for row in rows:
            item = await self.db.get_item(row["item_id"])
            if not item:
                continue
            # 找到该作品所属 UP（配置不存在则跳过）
            up = next((u for u in self.config.up_list if u.uid == item["up_uid"]), None)
            if up is None:
                continue
            try:
                oid = int(item.get("comment_oid") or 0)
            except (ValueError, TypeError):
                continue
            if not oid:
                continue
            comment_type = self.client.get_comment_type(item["item_type"])
            root_rpid = int(row["root_rpid"])
            # 查第1页拿权威 count（每基线1次请求，节流）
            try:
                resp = await self.client.get_sub_comments(oid, comment_type, root_rpid, 1)
            except Exception as e:
                logger.debug(f"子评论基线扫查失败 item={row['item_id']} root={root_rpid}: {e}")
                continue
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
            # 触发条件：count 变大 且 距上次翻 ≥ 2min；或基线过期强制翻
            need_check = (count > row["last_rcount"] and gap_sec >= SUB_COMMENT_MIN_INTERVAL) \
                or gap_sec >= self.config.intervals.sub_sweep_max_age
            if not need_check:
                continue
            logger.info(
                f"子评论基线扫查触发: item={row['item_id']} root={root_rpid} "
                f"count={count} 基线={row['last_rcount']} 距今={int(gap_sec)}s"
            )
            await self._process_sub_comments(
                oid, comment_type, root_rpid, item["item_id"], up,
                root_context=None, replies_count=count,
                # scene2 含粉丝帖：up_action.like 是发帖粉丝的赞，不检测"觉得很赞"
                check_up_liked=(item.get("source") != "scene2"),
                # 强制全量扫查：防"收集不全却误标基线"的截断漏检
                force_full=True,
            )

    # ==========================================================
    # 级别升降
    # ==========================================================

    def _check_level_transition(self, item: dict) -> int:
        """
        根据时间阈值判断监测级别。

        规则：
        - priority 项永远保持 Level 1
        - Level 1 → Level 2: 超过 level1_hours
        - Level 2 → Level 0: 超过 level2_hours

        Returns:
            应设置的新级别
        """
        current_level = item["monitor_level"]

        # 已归档的不再恢复
        if current_level == 0:
            return 0

        # priority 项不降级
        if item.get("is_priority"):
            return 1 if current_level != 1 else current_level

        # 计算年龄
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
            logger.info(f"降级: {item['item_id']} Level 1→2 (age={age_hours:.1f}h)")
            return 2

        if current_level == 2 and age_hours > t.level2_hours:
            logger.info(f"归档: {item['item_id']} Level 2→0 (age={age_hours:.1f}h)")
            return 0

        return current_level

    async def poll_level2(self, up: UpConfig):
        """
        低频轮询 Level 2 作品（24~120h 的旧作）。
        """
        if self._polling_l2:
            logger.debug(f"[{up.name}] 场景一L2轮询跳过（上一轮未完成）")
            return
        self._polling_l2 = True
        try:
            level2_items = await self.db.get_items_by_level(2, source="scene1")
            if not level2_items:
                return

            logger.debug(f"[{up.name}] 场景一L2轮询: {len(level2_items)} 个")

            for item in level2_items:
                await asyncio.sleep(random.uniform(
                    self.config.intervals.random_delay_min,
                    self.config.intervals.random_delay_max,
                ))
                try:
                    await self._poll_item(item, up)
                except Exception as e:
                    logger.error(f"场景一L2轮询失败 item={item['item_id']}: {e}")
        finally:
            self._polling_l2 = False

    async def recheck_all_levels(self, up: UpConfig):
        """
        定期重新分级：遍历所有活跃的 scene1 监测项，按 first_seen_at 重新算级别。
        priority 项永远保持 Level 1。
        """
        items = await self.db.get_active_items_by_source("scene1")
        changed = 0
        for item in items:
            new_level = self._check_level_transition(item)
            if new_level != item["monitor_level"]:
                await self.db.set_level(item["item_id"], new_level)
                changed += 1
        if changed > 0:
            logger.info(f"[{up.name}] 场景一重新分级: {changed} 条变更")


# ============================================================
# 快速测试入口
# ============================================================

async def _test():
    """快速验证场景一"""
    from config import Config

    cfg = Config("config.example.yaml")
    db = await Database(cfg.database.path).initialize()
    client = BiliClient()

    monitor = Scene1Monitor(db, client, cfg)

    for up in cfg.up_list:
        print(f"\n=== 测试 UP主: {up.name} ===")

        # 测试发现
        await monitor.discover(up)
        items = await db.get_items_by_level(1)
        print(f"[OK] discover: 当前 Level 1 监测项 {len(items)} 个")

        # 测试轮询（只轮询前2个，避免过多请求）
        if items:
            first = items[0]
            print(f"  轮询第一个: item_id={first['item_id'][:20]}... oid={first.get('comment_oid','')}")
            await monitor._poll_item(first, up)
            print(f"[OK] poll_item 完成")

        # 检查互动记录
        today = await db.get_today_interactions(up.uid)
        print(f"[OK] 今日互动记录: {len(today)} 条")

    await db.close()
    print("\n[OK] 场景一测试通过")


if __name__ == "__main__":
    asyncio.run(_test())
