# database.py
"""EchoWatch SQLite 异步数据库操作 —— 建表、CRUD、完整性检查"""

import asyncio
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

from logger_config import logger


# ============================================================
# 建表 SQL
# ============================================================

CREATE_MONITORED_ITEMS = """
CREATE TABLE IF NOT EXISTS monitored_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT UNIQUE NOT NULL,
    comment_oid     TEXT NOT NULL DEFAULT '',
    item_type       TEXT NOT NULL,
    source          TEXT NOT NULL,
    up_uid          TEXT NOT NULL,
    topic_id        INTEGER,
    first_seen_at   TEXT NOT NULL,
    last_polled_at  TEXT,
    monitor_level   INTEGER DEFAULT 1,
    is_priority     INTEGER DEFAULT 0,
    up_interacted   INTEGER DEFAULT 0,
    post_content    TEXT DEFAULT '',
    post_rich_content TEXT DEFAULT '',
    screenshot_pending INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now', 'localtime'))
)
"""

CREATE_INTERACTIONS = """
CREATE TABLE IF NOT EXISTS interactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    up_uid              TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    comment_id          TEXT UNIQUE NOT NULL,
    is_sub_reply        INTEGER DEFAULT 0,
    parent_content      TEXT,
    parent_author       TEXT,
    content             TEXT,
    rich_content        TEXT,
    up_liked            INTEGER DEFAULT 0,
    scene               TEXT NOT NULL,
    discovered_at       TEXT NOT NULL,
    notified_immediate  INTEGER DEFAULT 0,
    notified_digest     INTEGER DEFAULT 0
)
"""

CREATE_TOPIC_OFFSET = """
CREATE TABLE IF NOT EXISTS topic_offset (
    topic_id                    INTEGER PRIMARY KEY,
    offset_dynamic_id          TEXT,
    last_interacted_dynamic_id TEXT,
    updated_at                  TEXT DEFAULT (datetime('now', 'localtime'))
)
"""

# 子评论检测基线表：每个根评论一行，只存当前值（rcount 基线 + 上次翻页时间），
# 用于"rcount 变大且距上次翻 ≥ 2min"才翻子评论的节流判断
CREATE_SUB_COMMENT_BASELINE = """
CREATE TABLE IF NOT EXISTS sub_comment_baseline (
    item_id         TEXT NOT NULL,
    root_rpid       TEXT NOT NULL,
    last_rcount     INTEGER NOT NULL DEFAULT 0,
    last_check_ts   TEXT NOT NULL DEFAULT '1970-01-01 00:00:00',
    PRIMARY KEY (item_id, root_rpid)
)
"""

# 日报发送状态表：每天一行（日期主键）。
# 防"重启导致 _digest_sent_today 内存标记归零 → 同小时内补发日报"（2026-08-28 事故）
CREATE_DIGEST_STATE = """
CREATE TABLE IF NOT EXISTS digest_state (
    date        TEXT PRIMARY KEY,
    sent_at     TEXT DEFAULT (datetime('now', 'localtime'))
)
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_items_level ON monitored_items(monitor_level, source)",
    "CREATE INDEX IF NOT EXISTS idx_items_upuid ON monitored_items(up_uid)",
    "CREATE INDEX IF NOT EXISTS idx_items_polled ON monitored_items(last_polled_at)",
    "CREATE INDEX IF NOT EXISTS idx_interactions_upuid ON interactions(up_uid)",
    "CREATE INDEX IF NOT EXISTS idx_interactions_notified ON interactions(notified_immediate, notified_digest)",
    "CREATE INDEX IF NOT EXISTS idx_interactions_discovered ON interactions(discovered_at)",
]


# ============================================================
# 数据库操作类
# ============================================================

class Database:
    """异步 SQLite 数据库操作封装"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    # ----------------------------------------------------------
    # 初始化与生命周期
    # ----------------------------------------------------------

    async def initialize(self) -> "Database":
        """建表 + 启用 WAL 模式 + 完整性检查"""
        # 自动创建父目录
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # 完整性检查（连接前用标准库快速检查）
        if os.path.exists(self.db_path):
            if not self._integrity_check_sync():
                self._backup_and_rebuild()

        # 建立连接
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # WAL 模式（提升并发读性能）
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=OFF")

        # 建表
        await self._conn.execute(CREATE_MONITORED_ITEMS)
        await self._conn.execute(CREATE_INTERACTIONS)
        await self._conn.execute(CREATE_TOPIC_OFFSET)
        await self._conn.execute(CREATE_SUB_COMMENT_BASELINE)
        await self._conn.execute(CREATE_DIGEST_STATE)

        # 建索引
        for idx_sql in CREATE_INDEXES:
            await self._conn.execute(idx_sql)

        # 迁移：给已有数据库加列（新库建表已含，对旧库补加）
        await self._migrate_add_rich_content()
        await self._migrate_add_post_content()
        await self._migrate_add_post_rich_content()
        await self._migrate_add_parent_rich_content()
        await self._migrate_add_screenshot_pending()

        await self._conn.commit()
        logger.info(f"数据库初始化完成: {self.db_path}")
        return self

    async def close(self):
        """关闭数据库连接"""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("数据库连接已关闭")

    def _integrity_check_sync(self) -> bool:
        """同步检查数据库完整性，返回 True 表示正常"""
        try:
            conn = sqlite3.connect(self.db_path)
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            ok = result[0] == "ok"
            if not ok:
                logger.error(f"数据库完整性异常: {result}")
            return ok
        except Exception as e:
            logger.error(f"数据库完整性检查失败: {e}")
            return False

    def _backup_and_rebuild(self):
        """备份损坏的数据库并删除原文件（WAL 模式的 -wal/-shm 一并删除，防污染新建库）"""
        backup_path = self.db_path + f".backup_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(self.db_path, backup_path)
        os.remove(self.db_path)
        for suffix in ("-wal", "-shm"):
            side_file = self.db_path + suffix
            if os.path.exists(side_file):
                os.remove(side_file)
        logger.warning(f"数据库已损坏，已备份到 {backup_path} 并重建")
        logger.warning("注意：历史数据已丢失，监测将从头开始")

    async def _migrate_add_rich_content(self):
        """迁移：给已有数据库的 interactions 表加 rich_content 列（如不存在）"""
        columns = await self._conn.execute_fetchall("PRAGMA table_info(interactions)")
        col_names = [row[1] for row in columns]
        if "rich_content" not in col_names:
            await self._conn.execute("ALTER TABLE interactions ADD COLUMN rich_content TEXT")
            await self._conn.commit()
            logger.info("数据库迁移: 已添加 interactions.rich_content 列")

    async def _migrate_add_post_content(self):
        """迁移：给已有数据库的 monitored_items 表加 post_content 列（如不存在）"""
        columns = await self._conn.execute_fetchall("PRAGMA table_info(monitored_items)")
        col_names = [row[1] for row in columns]
        if "post_content" not in col_names:
            await self._conn.execute("ALTER TABLE monitored_items ADD COLUMN post_content TEXT DEFAULT ''")
            await self._conn.commit()
            logger.info("数据库迁移: 已添加 monitored_items.post_content 列")

    async def _migrate_add_post_rich_content(self):
        """迁移：给已有数据库的 monitored_items 表加 post_rich_content 列（如不存在）"""
        columns = await self._conn.execute_fetchall("PRAGMA table_info(monitored_items)")
        col_names = [row[1] for row in columns]
        if "post_rich_content" not in col_names:
            await self._conn.execute("ALTER TABLE monitored_items ADD COLUMN post_rich_content TEXT DEFAULT ''")
            await self._conn.commit()
            logger.info("数据库迁移: 已添加 monitored_items.post_rich_content 列")

    async def _migrate_add_parent_rich_content(self):
        """迁移：给已有数据库的 interactions 表加 parent_rich_content 列（如不存在）

        用途：被回复评论（parent）的表情包/图片富内容渲染（邮件里显示被回复内容的表情）。
        """
        columns = await self._conn.execute_fetchall("PRAGMA table_info(interactions)")
        col_names = [row[1] for row in columns]
        if "parent_rich_content" not in col_names:
            await self._conn.execute("ALTER TABLE interactions ADD COLUMN parent_rich_content TEXT")
            await self._conn.commit()
            logger.info("数据库迁移: 已添加 interactions.parent_rich_content 列")

    async def _migrate_add_screenshot_pending(self):
        """迁移：给已有数据库的 monitored_items 表加 screenshot_pending 列（如不存在）"""
        columns = await self._conn.execute_fetchall("PRAGMA table_info(monitored_items)")
        col_names = [row[1] for row in columns]
        if "screenshot_pending" not in col_names:
            await self._conn.execute(
                "ALTER TABLE monitored_items ADD COLUMN screenshot_pending INTEGER DEFAULT 0"
            )
            await self._conn.commit()
            logger.info("数据库迁移: 已添加 monitored_items.screenshot_pending 列")

    # ----------------------------------------------------------
    # monitored_items 表操作
    # ----------------------------------------------------------

    async def upsert_item(
        self,
        item_id: str,
        item_type: str,
        source: str,
        up_uid: str,
        comment_oid: str = "",
        topic_id: Optional[int] = None,
        is_priority: bool = False,
        pub_ts: int = 0,
        post_content: str = "",
        post_rich_content: str = "",
    ) -> bool:
        """
        插入或忽略监测项。
        返回 True 表示新插入，False 表示已存在。

        Args:
            pub_ts: 发布时间的 Unix 时间戳。若 > 0，用作 first_seen_at
                    （精确分级用）；否则用当前时间。
            post_content: 帖子正文（场景二用于邮件上下文展示）
            post_rich_content: 帖子富内容JSON（表情+图片+视频，邮件渲染用）
        """
        # first_seen_at 用发布时间，按真实年龄自然归档
        try:
            pub_ts_int = int(pub_ts)
        except (ValueError, TypeError):
            pub_ts_int = 0
        if pub_ts_int > 0:
            first_seen = datetime.fromtimestamp(pub_ts_int).strftime("%Y-%m-%d %H:%M:%S")
            # 初始级别按发布时间年龄计算，防止"旧帖入库即 L1 被立即轮询"：
            # 发现层（尤其空间动态API风控时的视频列表降级）会把历史投稿整批拉入，
            # 若 L1 硬编码并立即轮询，会把老帖评论区里的历史评论当新互动推送
            # （2026-08-26 事故：两年前联合投稿视频下的目标UP主历史评论被当新互动推送）。
            # 阈值与 config.thresholds 对齐（level1_hours=24 / level2_hours=120）。
            if is_priority:
                init_level = 1  # 置顶/priority 必须 L1，不进归档
            else:
                age_hours = (datetime.now() - datetime.fromtimestamp(pub_ts_int)).total_seconds() / 3600
                if age_hours > 120:
                    init_level = 0  # 发布超120h：入库即归档，不进轮询队列
                elif age_hours > 24:
                    init_level = 2
                else:
                    init_level = 1
        else:
            first_seen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            init_level = 1

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
        INSERT OR IGNORE INTO monitored_items
            (item_id, comment_oid, item_type, source, up_uid, topic_id,
             first_seen_at, last_polled_at, monitor_level, is_priority,
             post_content, post_rich_content)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self._conn.execute(sql, (
            item_id, comment_oid, item_type, source, up_uid, topic_id,
            first_seen, now, init_level, 1 if is_priority else 0,
            post_content, post_rich_content,
        ))
        await self._conn.commit()
        inserted = cursor.rowcount > 0
        if inserted:
            logger.debug(f"新增监测项: {item_id} (source={source}, priority={is_priority})")
        return inserted

    async def get_items_by_level(self, level: int, source: str = None) -> list[dict]:
        """获取指定监测级别的活跃项（新内容优先），可按来源过滤"""
        if source:
            sql = "SELECT * FROM monitored_items WHERE monitor_level = ? AND source = ? ORDER BY first_seen_at DESC"
            rows = await self._conn.execute_fetchall(sql, (level, source))
        else:
            sql = "SELECT * FROM monitored_items WHERE monitor_level = ? ORDER BY first_seen_at DESC"
            rows = await self._conn.execute_fetchall(sql, (level,))
        return [dict(r) for r in rows]

    async def get_active_items_by_source(self, source: str) -> list[dict]:
        """获取某来源下所有活跃监测项（monitor_level > 0），用于重新分级"""
        sql = "SELECT * FROM monitored_items WHERE source = ? AND monitor_level > 0 ORDER BY first_seen_at DESC"
        rows = await self._conn.execute_fetchall(sql, (source,))
        return [dict(r) for r in rows]

    async def claim_notified_immediate(self, interaction_id: int) -> bool:
        """
        原子认领一条即时通知（发送前调用，防双通道重复发送 2026-08-29）。

        30s 即时通知循环 与 priority 内联发送都可能取到同一条未通知互动，
        双方"发送成功后才标记"存在竞态窗口（P0-5）。此方法先抢行：
        UPDATE 带 notified_immediate=0 条件，抢到置 1，另一个通道抢不到即放弃。

        Returns:
            True=抢到（发送权在手）；False=已被其他通道认领/已发送
        """
        cursor = await self._conn.execute(
            "UPDATE interactions SET notified_immediate = 1 "
            "WHERE id = ? AND notified_immediate = 0",
            (interaction_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def release_notified_immediate(self, interaction_id: int):
        """释放认领失败：发送失败时回滚标记，下轮重试（保持"发送成功才标记"语义）"""
        await self._conn.execute(
            "UPDATE interactions SET notified_immediate = 0 WHERE id = ?",
            (interaction_id,)
        )
        await self._conn.commit()

    async def get_priority_items(self) -> list[dict]:
        """获取所有优先监测项（is_priority=1 且 level > 0，新内容优先）"""
        sql = "SELECT * FROM monitored_items WHERE is_priority = 1 AND monitor_level > 0 ORDER BY first_seen_at DESC"
        rows = await self._conn.execute_fetchall(sql)
        return [dict(r) for r in rows]

    async def get_priority_item_ids(self) -> set[str]:
        """获取所有优先监测项的 item_id 集合（用于快速过滤）"""
        sql = "SELECT item_id FROM monitored_items WHERE is_priority = 1 AND monitor_level > 0"
        rows = await self._conn.execute_fetchall(sql)
        return {row["item_id"] for row in rows}

    async def get_hot_items(self, up_uid: str, topic_id: int, limit: int = 20) -> list[dict]:
        """获取场景二某话题下最新的 N 条帖子（预测窗口用）"""
        sql = """
        SELECT * FROM monitored_items
        WHERE up_uid = ? AND topic_id = ? AND source = 'scene2' AND monitor_level > 0
        ORDER BY first_seen_at DESC
        LIMIT ?
        """
        rows = await self._conn.execute_fetchall(sql, (up_uid, topic_id, limit))
        return [dict(r) for r in rows]

    async def update_comment_oid(self, item_id: str, new_oid: str) -> bool:
        """
        更新监测项的评论区oid。
        只在新oid明显更短（<12位，真实aid）且旧oid过长（>=12位，动态ID）时更新。
        返回 True 表示已更新。
        """
        old = await self._conn.execute_fetchall(
            "SELECT comment_oid FROM monitored_items WHERE item_id = ?", (item_id,)
        )
        if not old:
            return False
        old_oid = old[0][0] or ""
        # 仅在旧oid过长（动态ID冒充）且新oid明显更短（真实aid）时更新
        if len(old_oid) >= 12 and len(new_oid) < len(old_oid):
            await self._conn.execute(
                "UPDATE monitored_items SET comment_oid = ? WHERE item_id = ?",
                (new_oid, item_id),
            )
            await self._conn.commit()
            logger.info(f"oid已修正: {item_id[:20]}... {old_oid} -> {new_oid}")
            return True
        return False

    async def clear_stale_priority(self, keep_ids: list[str]):
        """清除不在 keep_ids 中的 priority 标记。keep_ids 为空时清除全部。"""
        if keep_ids:
            placeholders = ",".join(["?"] * len(keep_ids))
            await self._conn.execute(
                f"UPDATE monitored_items SET is_priority = 0 WHERE is_priority = 1 AND item_id NOT IN ({placeholders})",
                keep_ids,
            )
        else:
            # 无保留项时清除所有 priority 标记（置顶消失场景）
            await self._conn.execute(
                "UPDATE monitored_items SET is_priority = 0 WHERE is_priority = 1"
            )
        await self._conn.commit()

    async def set_priority(self, item_id: str):
        """将已有监测项标记为优先（永不归档）"""
        await self._conn.execute(
            "UPDATE monitored_items SET is_priority = 1, monitor_level = 1 WHERE item_id = ?",
            (item_id,),
        )
        await self._conn.commit()

    async def update_poll_time(self, item_id: str):
        """更新最后轮询时间"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self._conn.execute(
            "UPDATE monitored_items SET last_polled_at = ? WHERE item_id = ?",
            (now, item_id)
        )
        await self._conn.commit()

    async def set_level(self, item_id: str, level: int):
        """修改监测级别"""
        await self._conn.execute(
            "UPDATE monitored_items SET monitor_level = ? WHERE item_id = ?",
            (level, item_id)
        )
        if level == 0:
            # 归档级联清理基线：L0 不可逆（_check_level_transition 对 L0 直接 return 0），
            # 归档后子评论基线无维护意义，删除防表膨胀（2026-08-29 审计）
            await self._conn.execute(
                "DELETE FROM sub_comment_baseline WHERE item_id = ?",
                (item_id,)
            )
        await self._conn.commit()
        logger.debug(f"监测级别变更: {item_id} -> Level {level}")

    async def set_up_interacted(self, item_id: str):
        """标记 UP主 已在此作品互动过"""
        await self._conn.execute(
            "UPDATE monitored_items SET up_interacted = 1 WHERE item_id = ?",
            (item_id,)
        )
        await self._conn.commit()

    async def get_item_post_content(self, item_id: str) -> str:
        """根据 item_id 查帖子正文（场景二邮件上下文用），不存在则返回空串"""
        row = await self._conn.execute_fetchall(
            "SELECT post_content, post_rich_content FROM monitored_items WHERE item_id = ?",
            (item_id,)
        )
        if row:
            return row[0]["post_content"] or ""
        return ""

    async def get_item_post_rich_content(self, item_id: str) -> str:
        """根据 item_id 查帖子富内容JSON（场景二邮件渲染用），不存在则返回空串"""
        row = await self._conn.execute_fetchall(
            "SELECT post_rich_content FROM monitored_items WHERE item_id = ?",
            (item_id,)
        )
        return row[0]["post_rich_content"] or "" if row else ""

    async def get_item_type(self, item_id: str) -> str:
        """根据 item_id 查动态类型（邮件链接生成用），不存在则返回空串"""
        row = await self._conn.execute_fetchall(
            "SELECT item_type FROM monitored_items WHERE item_id = ?",
            (item_id,)
        )
        return row[0]["item_type"] or "" if row else ""

    async def get_item_comment_oid(self, item_id: str) -> str:
        """根据 item_id 查评论区 oid（视频为 aid，动态为动态ID；邮件链接生成用）"""
        row = await self._conn.execute_fetchall(
            "SELECT comment_oid FROM monitored_items WHERE item_id = ?",
            (item_id,)
        )
        return row[0]["comment_oid"] or "" if row else ""

    async def get_item_by_comment_oid(self, comment_oid: str,
                                       source: str = None, up_uid: str = None) -> Optional[dict]:
        """
        按 comment_oid 查已有监测项（去重用）。

        不加参数：跨场景去重（同一评论区只应被一个场景监测，
        scene2/scene3 发现前先查此方法，占用则跳过）。
        加 source/up_uid 参数：场景内部去重（如 scene1 降级路径防 av 前缀与
        动态ID 双份入库，2026-08-29 审计）。

        Args:
            comment_oid: 评论区 oid
            source: 限定场景（None=不限）
            up_uid: 限定 UP 主（None=不限）
        """
        sql = "SELECT * FROM monitored_items WHERE comment_oid = ?"
        params: list = [comment_oid]
        if source:
            sql += " AND source = ?"
            params.append(source)
        if up_uid:
            sql += " AND up_uid = ?"
            params.append(up_uid)
        sql += " LIMIT 1"
        row = await self._conn.execute_fetchall(sql, tuple(params))
        return dict(row[0]) if row else None

    async def archive_stale(self, hours_threshold: int) -> int:
        """归档超时的监测项（Level > 0 设为 Level 0），返回归档数量"""
        cutoff = (datetime.now() - timedelta(hours=hours_threshold)).strftime("%Y-%m-%d %H:%M:%S")
        # 仅归档非优先项；先查出受影响 item（供基线级联清理用）
        rows = await self._conn.execute_fetchall(
            "SELECT item_id FROM monitored_items "
            "WHERE monitor_level > 0 AND is_priority = 0 AND first_seen_at < ?",
            (cutoff,)
        )
        item_ids = [r["item_id"] for r in rows]
        if not item_ids:
            return 0
        cursor = await self._conn.execute(
            "UPDATE monitored_items SET monitor_level = 0 "
            "WHERE monitor_level > 0 AND is_priority = 0 AND first_seen_at < ?",
            (cutoff,)
        )
        # 归档即不再轮询（L0 不可逆）：级联删除其子评论基线，
        # 防 sub_comment_baseline 随作品轮换单调膨胀（2026-08-29 审计）
        placeholders = ",".join("?" * len(item_ids))
        await self._conn.execute(
            f"DELETE FROM sub_comment_baseline WHERE item_id IN ({placeholders})",
            item_ids
        )
        await self._conn.commit()
        count = cursor.rowcount
        if count > 0:
            logger.info(f"归档了 {count} 条超时监测项（本级联清理其基线）")
        return count

    # ----------------------------------------------------------
    # interactions 表操作
    # ----------------------------------------------------------

    async def insert_interaction(self, data: dict) -> Optional[int]:
        """
        插入互动记录（comment_id 去重）。
        返回新行的 id（新互动），None 表示已存在（重复）。
        data 可选包含 rich_content 字段（JSON字符串，存表情/图片/超链接）
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
        INSERT OR IGNORE INTO interactions
            (up_uid, item_id, comment_id, is_sub_reply,
             parent_content, parent_author, parent_rich_content,
             content, rich_content, up_liked,
             scene, discovered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = await self._conn.execute(sql, (
            data.get("up_uid"),
            data.get("item_id"),
            data.get("comment_id"),
            1 if data.get("is_sub_reply") else 0,
            data.get("parent_content"),
            data.get("parent_author"),
            data.get("parent_rich_content"),
            data.get("content"),
            data.get("rich_content"),
            1 if data.get("up_liked") else 0,
            data.get("scene"),
            now,
        ))
        await self._conn.commit()
        inserted = cursor.rowcount > 0
        if inserted:
            logger.info(f"新互动: comment_id={data.get('comment_id')} scene={data.get('scene')}")
            return cursor.lastrowid
        return None

    async def get_interaction_by_id(self, id: int) -> Optional[dict]:
        """按 id 取单条互动记录，供即时推送使用"""
        sql = "SELECT * FROM interactions WHERE id = ?"
        row = await self._conn.execute_fetchall(sql, (id,))
        return dict(row[0]) if row else None

    # ----------------------------------------------------------
    # 子评论检测基线（rcount 节流）
    # ----------------------------------------------------------

    async def get_all_sub_baselines(self, source: str = None,
                                     up_uids: list = None,
                                     active_only: bool = True) -> list[dict]:
        """
        取子评论检测基线（兜底扫查用），默认过滤已归档项。

        2026-08-29 审计改造：
        - JOIN monitored_items 在 SQL 层过滤 monitor_level=0 的基线并支持
          场景/UP 过滤，替代旧的"全表返回+逐个 get_item 判断"
          （旧实现每轮 4k+ 行全拖回，DB 开销一分不减且表随归档单调膨胀）
        - SELECT 同时带出 item 字段（comment_oid/item_type/source/up_uid），
          调用方无需再逐条 get_item 查询

        Args:
            source: 只返回该场景的基线（None=不限）
            up_uids: 只返回这些 UP 的基线（None=不限）
            active_only: True=过滤 monitor_level=0 的已归档项（归档即停止监测）
        """
        sql = """
        SELECT b.item_id, b.root_rpid, b.last_rcount, b.last_check_ts,
               m.up_uid, m.comment_oid, m.item_type, m.source, m.monitor_level
        FROM sub_comment_baseline b
        JOIN monitored_items m ON m.item_id = b.item_id
        WHERE 1=1
        """
        params: list = []
        if active_only:
            sql += " AND m.monitor_level > 0"
        if source:
            sql += " AND m.source = ?"
            params.append(source)
        if up_uids:
            placeholders = ",".join("?" * len(up_uids))
            sql += f" AND m.up_uid IN ({placeholders})"
            params.extend(up_uids)
        rows = await self._conn.execute_fetchall(sql, tuple(params))
        return [dict(r) for r in rows]

    async def get_item(self, item_id: str) -> Optional[dict]:
        """按 item_id 取单个监测项（兜底扫查用）"""
        sql = "SELECT * FROM monitored_items WHERE item_id=?"
        row = await self._conn.execute_fetchall(sql, (item_id,))
        return dict(row[0]) if row else None

    async def get_sub_baseline(self, item_id: str, root_rpid) -> Optional[dict]:
        """
        取某根评论的子评论检测基线。

        Args:
            item_id: 所属作品ID
            root_rpid: 根评论 rpid

        Returns:
            {"last_rcount": int, "last_check_ts": str}，无记录返回 None（视为首次）
        """
        sql = "SELECT last_rcount, last_check_ts FROM sub_comment_baseline WHERE item_id=? AND root_rpid=?"
        row = await self._conn.execute_fetchall(sql, (item_id, str(root_rpid)))
        return dict(row[0]) if row else None

    async def upsert_sub_baseline(self, item_id: str, root_rpid, last_rcount: int, last_check_ts: str):
        """
        更新子评论检测基线（只存当前值，覆盖旧值，不保留历史）。

        Args:
            item_id: 所属作品ID
            root_rpid: 根评论 rpid
            last_rcount: 本次翻页时的 rcount（新基线）
            last_check_ts: 本次翻页时间
        """
        sql = """
        INSERT INTO sub_comment_baseline (item_id, root_rpid, last_rcount, last_check_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(item_id, root_rpid) DO UPDATE SET
            last_rcount = excluded.last_rcount,
            last_check_ts = excluded.last_check_ts
        """
        await self._conn.execute(sql, (item_id, str(root_rpid), last_rcount, last_check_ts))
        await self._conn.commit()

    async def mark_notified_immediate(self, ids: list[int]):
        """标记为已即时通知"""
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self._conn.execute(
            f"UPDATE interactions SET notified_immediate = 1 WHERE id IN ({placeholders})",
            ids
        )
        await self._conn.commit()

    async def mark_notified_digest(self, ids: list[int]):
        """标记为已纳入日报"""
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self._conn.execute(
            f"UPDATE interactions SET notified_digest = 1 WHERE id IN ({placeholders})",
            ids
        )
        await self._conn.commit()

    # ----------------------------------------------------------
    # 日报发送状态（防重启重复补发）
    # ----------------------------------------------------------

    async def is_digest_sent_today(self, date_str: str) -> bool:
        """今日日报是否已发送（用于重启后恢复判定，防同小时补发）"""
        row = await self._conn.execute_fetchall(
            "SELECT 1 FROM digest_state WHERE date = ?", (date_str,)
        )
        return bool(row)

    async def mark_digest_sent(self, date_str: str):
        """记录今日日报已发送"""
        await self._conn.execute(
            "INSERT OR REPLACE INTO digest_state (date) VALUES (?)", (date_str,)
        )
        await self._conn.commit()

    async def get_unnotified_immediate(self, scene: str = None, limit: int = 200,
                                       max_age_hours: int = 48) -> list[dict]:
        """
        获取未即时通知的互动，可选按场景过滤（scene1 逐条发，scene2 批量发）。

        2026-08-29 防护（P2-3/P2-4）：
        - limit=200：积压时不再一次全取逐条发信（原实现无上限，积压会撑爆一轮）
        - max_age_hours=48：超过 48h 的旧互动不再即时推送（只进日报，日报走
          get_unnotified_digest 全量不受此限）。防长时间关闭即时通知后重开
          触发历史补发风暴；不影响正常即时（新互动正常进入）
        """
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        if scene:
            sql = ("SELECT * FROM interactions WHERE notified_immediate = 0 AND scene = ? "
                   "AND discovered_at >= ? ORDER BY id LIMIT ?")
            rows = await self._conn.execute_fetchall(sql, (scene, cutoff, limit))
        else:
            sql = ("SELECT * FROM interactions WHERE notified_immediate = 0 "
                   "AND discovered_at >= ? ORDER BY id LIMIT ?")
            rows = await self._conn.execute_fetchall(sql, (cutoff, limit))
        return [dict(r) for r in rows]

    async def get_unnotified_digest(self, up_uid: str = None) -> list[dict]:
        """获取未纳入日报的互动（可选按 UP主 筛选）"""
        if up_uid:
            sql = "SELECT * FROM interactions WHERE notified_digest = 0 AND up_uid = ?"
            rows = await self._conn.execute_fetchall(sql, (up_uid,))
        else:
            sql = "SELECT * FROM interactions WHERE notified_digest = 0"
            rows = await self._conn.execute_fetchall(sql)
        return [dict(r) for r in rows]

    async def get_today_interactions(self, up_uid: str = None) -> list[dict]:
        """获取今天的互动记录（日报用）"""
        today = datetime.now().strftime("%Y-%m-%d")
        if up_uid:
            sql = "SELECT * FROM interactions WHERE discovered_at LIKE ? AND up_uid = ?"
            rows = await self._conn.execute_fetchall(sql, (f"{today}%", up_uid))
        else:
            sql = "SELECT * FROM interactions WHERE discovered_at LIKE ?"
            rows = await self._conn.execute_fetchall(sql, (f"{today}%",))
        return [dict(r) for r in rows]

    # ----------------------------------------------------------
    # topic_offset 表操作
    # ----------------------------------------------------------

    async def get_offset(self, topic_id: int) -> Optional[str]:
        """获取话题的偏移量"""
        row = await self._conn.execute_fetchall(
            "SELECT offset_dynamic_id FROM topic_offset WHERE topic_id = ?",
            (topic_id,)
        )
        return row[0]["offset_dynamic_id"] if row else None

    async def set_offset(self, topic_id: int, offset: str):
        """更新话题偏移量"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self._conn.execute(
            "INSERT OR REPLACE INTO topic_offset (topic_id, offset_dynamic_id, updated_at) "
            "VALUES (?, ?, ?)",
            (topic_id, offset, now)
        )
        await self._conn.commit()

    async def get_last_interacted(self, topic_id: int) -> Optional[str]:
        """获取话题中 UP主 最近一次互动的动态 ID（预测窗口用）"""
        row = await self._conn.execute_fetchall(
            "SELECT last_interacted_dynamic_id FROM topic_offset WHERE topic_id = ?",
            (topic_id,)
        )
        return row[0]["last_interacted_dynamic_id"] if row else None

    async def get_last_interacted_ts(self, topic_id: int) -> Optional[str]:
        """获取话题最近一次互动/偏移量写入时间（预测窗口清空判断用）"""
        row = await self._conn.execute_fetchall(
            "SELECT updated_at FROM topic_offset WHERE topic_id = ?",
            (topic_id,)
        )
        return row[0]["updated_at"] if row else None

    async def set_last_interacted(self, topic_id: int, dynamic_id: str):
        """更新话题中 UP主 最近互动位置"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self._conn.execute(
            "INSERT OR REPLACE INTO topic_offset "
            "(topic_id, last_interacted_dynamic_id, updated_at) "
            "VALUES (?, ?, ?)",
            (topic_id, dynamic_id, now)
        )
        await self._conn.commit()


# ============================================================
# 快速测试入口
# ============================================================

async def _test():
    """快速验证数据库模块"""
    import tempfile
    db_path = os.path.join(tempfile.gettempdir(), "echowatch_test.db")
    db = await Database(db_path).initialize()

    try:
        # 测试插入
        assert await db.upsert_item("dyn_001", "dynamic", "scene1", "000000000", is_priority=True)
        assert not await db.upsert_item("dyn_001", "dynamic", "scene1", "000000000")  # 重复
        print("[OK] upsert_item 通过")

        # 测试互动去重
        data = {"up_uid": "000000000", "item_id": "dyn_001", "comment_id": "rpid_123",
                "is_sub_reply": False, "content": "测试评论",
                "rich_content": '{"emote":{},"pictures":[],"jump_url":{}}',
                "up_liked": False, "scene": "scene1"}
        assert await db.insert_interaction(data)
        assert not await db.insert_interaction(data)  # 重复
        print("[OK] insert_interaction 去重通过")

        # 测试查询
        items = await db.get_priority_items()
        assert len(items) == 1
        print("[OK] get_priority_items 通过")

        # 测试通知标记
        unseen = await db.get_unnotified_immediate()
        assert len(unseen) == 1
        await db.mark_notified_immediate([unseen[0]["id"]])
        unseen = await db.get_unnotified_immediate()
        assert len(unseen) == 0
        print("[OK] 通知标记通过")

        # 测试偏移量
        assert await db.get_offset(12345) is None
        await db.set_offset(12345, "dyn_latest")
        assert await db.get_offset(12345) == "dyn_latest"
        print("[OK] topic_offset 通过")

        # 测试归档
        count = await db.archive_stale(9999)  # 极短阈值，归档所有
        print(f"[OK] archive_stale: 归档 {count} 条")

    finally:
        await db.close()
        os.remove(db_path)
        print("[OK] 所有数据库测试通过")


if __name__ == "__main__":
    asyncio.run(_test())
