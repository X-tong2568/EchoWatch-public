# -*- coding: utf-8 -*-
"""EchoWatch GitHub 归档离线测试（全部走 DRY_RUN，不碰真实仓库）"""

import asyncio
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from database import Database
from echowatch_archive import ArchiveConfig, EchoArchive, GitDataWriter, build_url


class FakeDB:
    """归档模块只需要这几个 DB 方法"""

    def __init__(self, priority_ids=None):
        self.frozen = []
        self.priority_ids = priority_ids or set()

    async def add_never_archive(self, item_id, rpid="", reason="", since=""):
        self.frozen.append((item_id, rpid, reason, since))

    async def get_priority_item_ids(self):
        return self.priority_ids

    async def get_item_id_by_comment_id(self, comment_id):
        return "D1" if str(comment_id).startswith("10") else "D2"

    async def get_notified_since(self, hours=48, limit=500):
        return []


def make_item(rpid, content="hello", item_id="D1", scene="scene1",
              discovered_at="2026-11-10 12:00:00", **kwargs):
    """构造一条最小互动"""
    data = {
        "comment_id": str(rpid),
        "item_id": item_id,
        "item_type": "DYNAMIC_TYPE_DRAW",
        "scene": scene,
        "discovered_at": discovered_at,
        "content": content,
        "rich_content": "{}",
        "is_sub_reply": 0,
        "up_liked": 0,
        "comment_author": "UP",
    }
    data.update(kwargs)
    return data


class ArchiveTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="ew_archive_test_")
        self.tmp = Path(self.tmp_obj.name)
        self.screens = self.tmp / "screens"
        self.screens.mkdir()

    def tearDown(self):
        self.tmp_obj.cleanup()

    def make_archive(self, cursor=None, db=None):
        cfg = ArchiveConfig(
            enabled=True,
            dry_run=True,
            preview_dir=str(self.tmp / "preview"),
            base_dir=self.tmp,
            screenshot_dir=str(self.screens),
            flush_seconds=180,
        )
        arch = EchoArchive(cfg, db or FakeDB())
        if cursor is not None:
            state_dir = Path(cfg.preview_dir) / "_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "pinned_cursor.json").write_text(
                json.dumps(cursor, ensure_ascii=False), encoding="utf-8"
            )
        return arch

    async def test_idempotent_and_restart(self):
        arch = self.make_archive()
        item = make_item("100")
        await arch.enqueue(item)
        await arch.enqueue(item)
        self.assertEqual(len(arch._queue), 1)
        self.assertTrue(await arch.flush("test"))

        state = json.loads(
            (Path(arch.cfg.preview_dir) / "comments/_state/archived_rpids.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(state["rpids"], ["100"])

        # 重启：新实例从账本恢复，重复入队最终只保留一份
        arch2 = self.make_archive()
        await arch2.enqueue(item)
        await arch2.flush("restart")
        md = (Path(arch2.cfg.preview_dir) / "comments/2026-11.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(md.count("<!-- ew:rpid:100 -->"), 1)

    async def test_monthly_header_and_table(self):
        """互动按月份归档，表头与空间归档一致，不生成日目录"""
        arch = self.make_archive()
        await arch.enqueue(make_item("120", content="第一条"))
        await arch.enqueue(make_item(
            "121", content="第二条", discovered_at="2026-11-11 09:00:00"
        ))
        self.assertTrue(await arch.flush("monthly"))
        root = Path(arch.cfg.preview_dir)
        md = (root / "comments/2026-11.md").read_text(encoding="utf-8")
        self.assertIn("# EchoWatch 互动归档 · 2026-11", md)
        self.assertIn("| 时间 | 类别 | 摘要 |", md)
        self.assertIn("本月共 2 条", md)
        self.assertIn("第一条", md)
        self.assertIn("第二条", md)
        self.assertFalse((root / "comments/2026/11").exists())

    async def test_root_readme_comments_month_index(self):
        """根 README 的粉丝互动月份随新互动归档即时更新，其他分类不动"""
        arch = self.make_archive()
        preview = Path(arch.cfg.preview_dir)
        preview.mkdir(parents=True, exist_ok=True)
        (preview / "README.md").write_text(
            "# 通知归档\n\n"
            "<!-- ARCHIVE_INDEX_START -->\n"
            "### 空间归档\n- [2026-10](space/2026-10.md)\n\n"
            "### 粉丝互动\n- [EchoWatch 互动归档](comments/README.md)\n\n"
            "<!-- ARCHIVE_INDEX_END -->\n",
            encoding="utf-8",
        )

        await arch.enqueue(make_item("110"))
        self.assertTrue(await arch.flush("readme"))
        readme = (preview / "README.md").read_text(encoding="utf-8")
        self.assertIn("- [2026-11](comments/2026-11.md) · [CSV](comments/2026-11.csv)", readme)
        self.assertIn("- [EchoWatch 互动归档](comments/README.md)", readme)
        self.assertIn("### 空间归档", readme)
        self.assertIn("- [2026-10](space/2026-10.md)", readme)

        # 同月再次归档不重复加月份
        await arch.enqueue(make_item("111"))
        await arch.flush("readme2")
        readme2 = (preview / "README.md").read_text(encoding="utf-8")
        self.assertEqual(readme2.count("- [2026-11](comments/2026-11.md) · [CSV](comments/2026-11.csv)"), 1)

    async def test_markdown_escaping(self):
        arch = self.make_archive()
        await arch.enqueue(make_item(
            "101",
            content="~旧话题~ |管道| <b>x</b> `code`",
        ))
        await arch.flush("escape")
        md = (Path(arch.cfg.preview_dir) / "comments/2026-11.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("～旧话题～", md)
        self.assertIn("\\|管道\\|", md)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", md)
        self.assertNotIn("<b>x</b>", md)

    async def test_emoji_not_double_bracketed(self):
        arch = self.make_archive()
        arch.cfg  # noqa
        item = make_item(
            "102",
            content="[doge]",
            rich_content=json.dumps({"emote": {"doge": {}}, "pictures": []}),
        )
        await arch.enqueue(item)
        await arch.flush("emoji")
        md = (Path(arch.cfg.preview_dir) / "comments/2026-11.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("[doge]", md)
        self.assertNotIn("[[doge]]", md)

    async def test_emoji_images_in_md(self):
        """已有表情资源时，MD 里显示图片，不再留 [doge] 文本"""
        arch = self.make_archive()
        url = "https://example.com/doge.png"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        asset = Path(arch.cfg.preview_dir) / "comments/assets/emoji" / f"{digest}.webp"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"fake-webp")

        item = make_item(
            "103",
            content="[doge]",
            rich_content=json.dumps({
                "emote": {"[doge]": {"url": url}},
                "pictures": [],
            }),
        )
        await arch.enqueue(item)
        await arch.flush("emoji_image")
        md = (Path(arch.cfg.preview_dir) / "comments/2026-11.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"![doge](assets/emoji/{digest}.webp)", md)
        self.assertIn(f"**UP说**：![doge](assets/emoji/{digest}.webp)", md)

    async def test_url_builder(self):
        self.assertTrue(build_url("BV1xx411c7mD", "DYNAMIC_TYPE_AV", "", "1")
                        .startswith("https://www.bilibili.com/video/BV1xx411c7mD"))
        self.assertIn("/video/av123", build_url("123", "DYNAMIC_TYPE_AV", "123", "1"))
        self.assertIn("/read/cv998877", build_url("cv998877", "", "", "1"))
        self.assertIn("/read/cv123", build_url("123", "DYNAMIC_TYPE_ARTICLE", "", "1"))
        self.assertIn("t.bilibili.com/456", build_url("456", "DYNAMIC_TYPE_DRAW", "", "1"))

    async def test_pinned_body_not_archived(self):
        """置顶评论本体（当前 + 上一任）由通知归档负责，EchoWatch 不重复归档"""
        cursor = {
            "pinned_dynamic_id": "D1",
            "pinned_rpid": "100",
            "since": "2026-11-10 10:00:00",
            "prev_rpid": "099",
            "prev_since": "2026-11-09 10:00:00",
        }
        arch = self.make_archive(cursor=cursor)
        await arch.enqueue(make_item("100", content="pinned body"))
        await arch.enqueue(make_item(
            "099", content="prev pinned body", discovered_at="2026-11-11 10:00:00"
        ))
        self.assertTrue(await arch.flush("pinned"))
        root = Path(arch.cfg.preview_dir)
        self.assertFalse((root / "comments/2026-11.md").exists())
        self.assertFalse((root / "pinned/_thread/2026-11-10.md").exists())

    async def test_priority_main_skipped_sub_archived(self):
        """置顶动态有更新的主评论不归档；置顶动态子评论汇总仍归档"""
        db = FakeDB(priority_ids={"D1"})
        arch = self.make_archive(cursor={
            "pinned_dynamic_id": "D1", "pinned_rpid": "100",
        }, db=db)
        await arch.enqueue(make_item("100", content="pinned body", item_id="D1"))
        await arch.enqueue(make_item(
            "101", content="sub reply", item_id="D1", is_sub_reply=1,
            discovered_at="2026-11-10 13:00:00",
        ))
        self.assertTrue(await arch.flush("priority"))
        root = Path(arch.cfg.preview_dir)
        comments = (root / "comments/2026-11.md").read_text(encoding="utf-8")
        thread = (root / "pinned/_thread/2026-11-10.md").read_text(encoding="utf-8")
        self.assertNotIn("<!-- ew:rpid:100 -->", comments)
        self.assertIn("<!-- ew:rpid:101 -->", comments)
        self.assertNotIn("<!-- ew:rpid:100 -->", thread)
        self.assertIn("<!-- ew:echo:101 -->", thread)

    async def test_switch_race_migration(self):
        """曾按普通互动归档、后成为置顶本体的块，要从月文件和 CSV 里删掉"""
        arch = self.make_archive(cursor={
            "pinned_dynamic_id": "D1", "pinned_rpid": "", "since": "",
        })
        await arch.enqueue(make_item("100", content="first"))
        await arch.flush("before_cursor")
        self.assertFalse((Path(arch.cfg.preview_dir) / "pinned/_thread/2026-11-10.md").exists())

        # 模拟通知归档游标随后补上 rpid；再来一条触发第二次 flush
        state_dir = Path(arch.cfg.preview_dir) / "_state"
        (state_dir / "pinned_cursor.json").write_text(json.dumps({
            "pinned_dynamic_id": "D1", "pinned_rpid": "100",
            "since": "2026-11-10 13:00:00",
        }), encoding="utf-8")
        arch._cursor_cache = (0.0, {})
        await arch.enqueue(make_item("101", content="echo", discovered_at="2026-11-10 13:01:00"))
        await arch.flush("after_cursor")

        root = Path(arch.cfg.preview_dir)
        comments = (root / "comments/2026-11.md").read_text(encoding="utf-8")
        csv_text = (root / "comments/2026-11.csv").read_text(encoding="utf-8-sig")
        thread = (root / "pinned/_thread/2026-11-10.md").read_text(encoding="utf-8")
        self.assertNotIn("<!-- ew:rpid:100 -->", comments)
        self.assertNotIn("<!-- ew:pointer:100 -->", comments)
        self.assertNotIn("100", csv_text.splitlines()[1])  # 只剩 101 那一行
        self.assertNotIn("<!-- ew:rpid:100 -->", thread)
        self.assertIn("<!-- ew:echo:101 -->", thread)
        self.assertNotIn("<!-- ew:echo:100 -->", thread)

    async def test_same_rpid_content_drift_is_dropped(self):
        arch = self.make_archive()
        await arch.enqueue(make_item("100", content="v1"))
        await arch.flush("v1")
        await arch.enqueue(make_item("100", content="v2"))
        self.assertEqual(len(arch._queue), 0)
        await arch.flush("v2")
        md = (Path(arch.cfg.preview_dir) / "comments/2026-11.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(md.count("<!-- ew:rpid:100 -->"), 1)

    async def test_missing_screenshot_degrades(self):
        arch = self.make_archive()
        await arch.enqueue(make_item("100", content="no image"))
        self.assertTrue(await arch.flush("no_image"))
        md = (Path(arch.cfg.preview_dir) / "comments/2026-11.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("![原帖截图]", md)

    async def test_concurrent_flush_no_duplicates(self):
        arch = self.make_archive()
        for i in range(20):
            await arch.enqueue(make_item(str(200 + i), content=f"c{i}"))
        await asyncio.gather(*[arch.flush("concurrent") for _ in range(8)])
        root = Path(arch.cfg.preview_dir)
        md = (root / "comments/2026-11.md").read_text(encoding="utf-8")
        state = json.loads(
            (root / "comments/_state/archived_rpids.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(set(state["rpids"])), 20)
        for i in range(20):
            self.assertEqual(md.count(f"<!-- ew:rpid:{200 + i} -->"), 1)

    async def test_network_failure_keeps_queue(self):
        arch = self.make_archive()

        class FailingWriter:
            def get_base(self):
                raise RuntimeError("network down")

        arch.writer = FailingWriter()
        await arch.enqueue(make_item("100"))
        self.assertFalse(await arch.flush("fail"))
        self.assertEqual(len(arch._queue), 1)

    async def test_author_and_root_readme_untouched(self):
        arch = self.make_archive()
        await arch.enqueue(make_item("100"))
        await arch.flush("author")
        root = Path(arch.cfg.preview_dir)
        commit = json.loads((root / "_preview_commit.json").read_text(encoding="utf-8"))
        self.assertIn("EchoWatch Archive", commit["author"])
        self.assertFalse((root / "README.md").exists())
        self.assertTrue((root / "comments/README.md").exists())


class NeverArchiveDBTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="ew_db_test_")
        self.db = await Database(str(Path(self.tmp_obj.name) / "test.db")).initialize()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp_obj.cleanup()

    async def test_never_archive_skips_stale_and_baseline(self):
        await self.db.upsert_item("D1", "DYNAMIC_TYPE_DRAW", "scene1", "up1")
        await self.db.upsert_item("D2", "DYNAMIC_TYPE_DRAW", "scene1", "up1")
        await self.db.upsert_sub_baseline("D1", "root1", 1, "2026-01-01 00:00:00")
        await self.db.upsert_sub_baseline("D2", "root2", 1, "2026-01-01 00:00:00")
        await self.db.add_never_archive("D1", "100", "pinned", "2026-01-01 00:00:00")

        # 把两条都改成很老，触发 archive_stale
        await self.db._conn.execute(
            "UPDATE monitored_items SET first_seen_at = '2020-01-01 00:00:00'"
        )
        await self.db._conn.commit()

        count = await self.db.archive_stale(1)
        self.assertEqual(count, 1)

        rows = await self.db._conn.execute_fetchall(
            "SELECT item_id, monitor_level FROM monitored_items ORDER BY item_id"
        )
        levels = {r["item_id"]: r["monitor_level"] for r in rows}
        self.assertEqual(levels["D1"], 1)
        self.assertEqual(levels["D2"], 0)

        baseline_rows = await self.db._conn.execute_fetchall(
            "SELECT item_id FROM sub_comment_baseline ORDER BY item_id"
        )
        self.assertEqual([r["item_id"] for r in baseline_rows], ["D1"])

        # 显式 set_level(0) 也不能冻结项归档
        await self.db.set_level("D1", 0)
        row = await self.db._conn.execute_fetchall(
            "SELECT monitor_level FROM monitored_items WHERE item_id='D1'"
        )
        self.assertEqual(row[0]["monitor_level"], 1)

    async def test_promote_migrates_never_archive(self):
        """av 降级项升级为真实动态 ID 时，冻结表也要跟着迁移"""
        await self.db.upsert_item("av123", "DYNAMIC_TYPE_AV", "scene1", "up1")
        await self.db.add_never_archive("av123", "100", "pinned", "2026-01-01 00:00:00")
        await self.db.promote_item_id("av123", "999")
        rows = await self.db._conn.execute_fetchall(
            "SELECT item_id, rpid FROM never_archive"
        )
        self.assertEqual([(r["item_id"], r["rpid"]) for r in rows], [("999", "100")])


class GitDataWriterTest(unittest.TestCase):
    """GitHub 请求参数回归测试（不联网）"""

    def test_raw_accept_goes_to_headers(self):
        """读 blob 的 raw Accept 必须进请求头，不能透传给 requests"""
        from unittest.mock import Mock, patch

        cfg = ArchiveConfig(token="test-token", owner="o", repo="r")
        writer = GitDataWriter(cfg)
        resp = Mock(status_code=200)
        with patch("echowatch_archive.requests.request", return_value=resp) as req:
            writer._api("GET", "/x", accept="application/vnd.github.raw")
        _, kwargs = req.call_args
        self.assertEqual(kwargs["headers"]["Accept"], "application/vnd.github.raw")


if __name__ == "__main__":
    unittest.main()
