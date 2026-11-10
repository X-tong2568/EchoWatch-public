# echowatch_archive.py
"""
EchoWatch GitHub 互动归档。

设计要点：
    1. 只写归档仓的 comments/、pinned/_thread/，以及根 README 归档索引里的
       「粉丝互动」月份；不碰 dynamics/、live/ 和其他索引分类。
    2. 提交通道用 Git Data API（blob -> tree -> commit -> PATCH ref），不用 Contents API
       逐文件提交，避免 409 时静默覆盖。
    3. 幂等键是 interactions.comment_id；comments/_state/archived_rpids.json 是最终账本。
    4. 归档钩子挂在“邮件发送成功之后”，归档失败绝不冒泡到推送链路。
    5. 置顶重叠按通知归档写入的 _state/pinned_cursor.json 路由：
       rpid == pinned_rpid 的正文只进 pinned/_thread/，comments/ 只留指针。
    6. 根 README 由通知归档和 EchoWatch 共同维护，但各自只改自己的索引块；
       冲突时 Git Data API 返回 NonFastForward，flush 会重读 base 后重试。
"""

import asyncio
import base64
import csv
import hashlib
import html
import io
import json
import os
import re
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from logger_config import logger

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


SCENE_LABELS = {
    "scene1": "自身作品",
    "scene2": "话题互动",
    "scene3": "切片视频",
    "scene4": "其他UP动态",
}

COMMENTS_STATE_PATH = "comments/_state/archived_rpids.json"
PINNED_CURSOR_PATH = "_state/pinned_cursor.json"
THREAD_ANCHOR = "<!-- echowatch:thread -->"
INDEX_ANCHOR = "<!-- echowatch:index -->"
ROOT_INDEX_START = "<!-- ARCHIVE_INDEX_START -->"
ROOT_INDEX_END = "<!-- ARCHIVE_INDEX_END -->"
COMMENTS_SECTION = "### 粉丝互动"
MD_HEADER_START = "<!-- ARCHIVE_HEADER_START -->"
MD_HEADER_END = "<!-- ARCHIVE_HEADER_END -->"
MD_SECTIONS_START = "<!-- ARCHIVE_SECTIONS_START -->"
MD_SECTIONS_END = "<!-- ARCHIVE_SECTIONS_END -->"

CSV_FIELDS = [
    "discovered_at",
    "scene",
    "scene_label",
    "rpid",
    "is_sub",
    "up_liked",
    "priority",
    "item_id",
    "item_type",
    "parent_author",
    "parent_content",
    "content",
    "has_image",
    "url",
]


# ============================================================
# 配置
# ============================================================

@dataclass
class ArchiveConfig:
    """归档配置；字段名与通知归档的 config_github.py 保持一致"""

    enabled: bool = False
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    token: str = ""
    timeout: int = 20
    dry_run: bool = True
    image_max_size: int = 1280
    webp_quality: int = 80
    anonymize_nick: bool = False
    never_archive_pinned: bool = True
    flush_seconds: int = 180
    author_name: str = "echowatch-bot"
    author_email: str = "echowatch@users.noreply.github.com"
    max_queue: int = 2000
    screenshot_dir: str = "sent_emails"
    preview_dir: str = "data/archive_preview"
    base_dir: Path = field(default_factory=lambda: Path("."))

    @classmethod
    def load(cls, yaml_section: Optional[dict] = None,
             base_dir: Optional[Path] = None) -> "ArchiveConfig":
        """
        加载顺序：默认值 -> config_github.py -> YAML github_archive 段 -> 环境变量。
        环境变量优先级最高，方便服务器临时覆盖。
        """
        base = Path(base_dir or Path(__file__).resolve().parent)
        cfg = cls(base_dir=base)

        # 1) config_github.py（gitignored）
        cfg_file = base / "config_github.py"
        if cfg_file.exists():
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location("_ew_config_github", str(cfg_file))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                for name in (
                    "GITHUB_ARCHIVE_ENABLED", "GITHUB_ARCHIVE_OWNER",
                    "GITHUB_ARCHIVE_REPO", "GITHUB_ARCHIVE_BRANCH",
                    "GITHUB_ARCHIVE_TOKEN", "GITHUB_ARCHIVE_TIMEOUT",
                    "GITHUB_ARCHIVE_DRY_RUN", "GITHUB_ARCHIVE_IMAGE_MAX_SIZE",
                    "GITHUB_ARCHIVE_WEBP_QUALITY",
                ):
                    if hasattr(module, name):
                        setattr(cfg, _config_field(name), getattr(module, name))
                for name in ("ARCHIVE_ANONYMIZE_NICK", "ARCHIVE_NEVER_ARCHIVE_PINNED",
                             "ARCHIVE_FLUSH_SECONDS"):
                    if hasattr(module, name):
                        setattr(cfg, _config_field(name), getattr(module, name))
            except Exception as e:
                logger.warning(f"读取 config_github.py 失败，使用默认值: {e}")

        # 2) YAML github_archive 段（小写字段）
        if yaml_section:
            for key, value in yaml_section.items():
                field_name = _config_field(key)
                if hasattr(cfg, field_name):
                    setattr(cfg, field_name, value)

        # 3) 环境变量（同名大写）
        env_map = {
            "GITHUB_ARCHIVE_ENABLED": "enabled",
            "GITHUB_ARCHIVE_OWNER": "owner",
            "GITHUB_ARCHIVE_REPO": "repo",
            "GITHUB_ARCHIVE_BRANCH": "branch",
            "GITHUB_ARCHIVE_TOKEN": "token",
            "GITHUB_ARCHIVE_TIMEOUT": "timeout",
            "GITHUB_ARCHIVE_DRY_RUN": "dry_run",
            "GITHUB_ARCHIVE_IMAGE_MAX_SIZE": "image_max_size",
            "GITHUB_ARCHIVE_WEBP_QUALITY": "webp_quality",
            "ARCHIVE_ANONYMIZE_NICK": "anonymize_nick",
            "ARCHIVE_NEVER_ARCHIVE_PINNED": "never_archive_pinned",
            "ARCHIVE_FLUSH_SECONDS": "flush_seconds",
        }
        for env_name, field_name in env_map.items():
            if env_name in os.environ:
                setattr(cfg, field_name, _cast_value(
                    os.environ[env_name], getattr(cfg, field_name)
                ))

        cfg.screenshot_dir = str(base / cfg.screenshot_dir)
        cfg.preview_dir = str(base / cfg.preview_dir)
        return cfg


def _config_field(name: str) -> str:
    """把通知归档风格的大写字段名转成 dataclass 字段名"""
    name = name.lower()
    for prefix in ("github_archive_", "archive_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


def _cast_value(raw: str, default: Any) -> Any:
    """按默认值类型转换环境变量"""
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    return raw


# ============================================================
# 文本 / URL 工具
# ============================================================

def esc_md(text: Any) -> str:
    """Markdown 转义：波浪号、管道、尖括号、反引号"""
    s = str(text or "")
    s = s.replace("~", "～")
    s = s.replace("|", "\\|")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("`", "\\`")
    return s


def esc_nick(name: Any, cfg: ArchiveConfig) -> str:
    """昵称转义；开启匿名时哈希成“粉丝#abcd”"""
    raw = str(name or "").strip()
    if not raw:
        return ""
    if cfg.anonymize_nick:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:4]
        return f"粉丝#{digest}"
    return esc_md(raw)


def flatten_content(content: Any, rich_content: Any) -> str:
    """把正文 + rich_content 展平成可读文本，表情保留 [xx] 占位"""
    text = str(content or "")
    try:
        rich = json.loads(rich_content) if rich_content else {}
    except (TypeError, ValueError):
        rich = {}

    emotes = rich.get("emote") or {}
    if isinstance(emotes, dict):
        for key in emotes:
            key = str(key)
            bare = key.strip("[]")
            if f"[{bare}]" in text:
                continue
            if bare and bare in text:
                text = text.replace(bare, f"[{bare}]")

    pictures = rich.get("pictures") or []
    if pictures:
        text = (text.rstrip() + " " + " ".join(["[图片]"] * len(pictures))).strip()

    return esc_md(text)


def build_url(item_id: Any, item_type: Any = "", comment_oid: Any = "",
              rpid: Any = "") -> str:
    """按 BV / cv / 视频 av / 动态 生成直达链接；先判 BV 再判数字"""
    item_id = str(item_id or "")
    item_type = str(item_type or "")
    rpid = str(rpid or "")
    comment_oid = str(comment_oid or "")

    if item_id.startswith("BV"):
        return f"https://www.bilibili.com/video/{item_id}#reply{rpid}"
    if item_id.lower().startswith("cv"):
        return f"https://www.bilibili.com/read/{item_id}"
    if "CV" in item_type.upper() or "ARTICLE" in item_type.upper():
        return f"https://www.bilibili.com/read/cv{item_id}"

    if item_id.lower().startswith("av"):
        aid = item_id[2:]
        return f"https://www.bilibili.com/video/av{aid}#reply{rpid}"
    if "AV" in item_type.upper():
        aid = comment_oid or item_id
        return f"https://www.bilibili.com/video/av{aid}#reply{rpid}"

    return f"https://t.bilibili.com/{item_id}#reply{rpid}"


def git_blob_sha(data: bytes) -> str:
    """本地计算 Git blob SHA，用于在提交前剔除未变化文件"""
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data).hexdigest()


def _to_webp(raw: bytes, max_size: int, quality: int) -> Optional[bytes]:
    """JPEG/PNG -> WebP，长边限制；没有 Pillow 时返回 None"""
    if not PIL_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((max_size, max_size))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="WEBP", quality=quality)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"图片转 WebP 失败: {e}")
        return None


# ============================================================
# Git Data API
# ============================================================

class GitDataError(Exception):
    """GitHub API 错误"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"GitHub API {status}: {message}")

    @property
    def permanent(self) -> bool:
        return self.status in (401, 403, 404)


class NonFastForward(GitDataError):
    """ref 已被其他提交推进，需要重读 base 后重试"""


@dataclass
class RepoBase:
    """一次 flush 的仓库快照：head + tree + 路径到 blob sha 映射"""

    head_sha: str
    tree_sha: str
    tree_map: Dict[str, str]
    reader: Any

    def read(self, rel_path: str) -> Optional[str]:
        return self.reader(rel_path)


class GitDataWriter:
    """Git Data API 写入器（只用于真仓库）"""

    API = "https://api.github.com"

    def __init__(self, cfg: ArchiveConfig):
        self.cfg = cfg
        if not cfg.token:
            raise GitDataError(401, "未配置 GITHUB_ARCHIVE_TOKEN")

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "EchoWatch-archive",
        }

    def _api(self, method: str, path: str, allow_404: bool = False,
             **kwargs) -> Optional[requests.Response]:
        try:
            resp = requests.request(
                method, f"{self.API}{path}", headers=self._headers(),
                timeout=self.cfg.timeout, **kwargs
            )
        except Exception as e:
            raise RuntimeError(f"GitHub 请求失败: {e}")

        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except Exception:
                message = (resp.text or "")[:200]
            if resp.status_code == 422:
                raise NonFastForward(resp.status_code, message)
            raise GitDataError(resp.status_code, message)
        return resp

    def check_private(self) -> None:
        """非 DRY_RUN 时强制检查归档仓是 Private"""
        resp = self._api("GET", f"/repos/{self.cfg.owner}/{self.cfg.repo}")
        if not resp or not resp.json().get("private"):
            raise GitDataError(403, "归档仓不是 Private，拒绝写入")

    def get_base(self) -> RepoBase:
        """读取当前 ref / tree / 文件 sha 映射"""
        ref = self._api(
            "GET", f"/repos/{self.cfg.owner}/{self.cfg.repo}/git/ref/heads/{self.cfg.branch}",
            allow_404=True,
        )
        if ref is None:
            raise GitDataError(404, "归档仓分支不存在")
        head_sha = ref.json()["object"]["sha"]

        commit = self._api(
            "GET", f"/repos/{self.cfg.owner}/{self.cfg.repo}/git/commits/{head_sha}"
        ).json()
        tree_sha = commit["tree"]["sha"]

        tree = self._api(
            "GET",
            f"/repos/{self.cfg.owner}/{self.cfg.repo}/git/trees/{tree_sha}?recursive=1",
        ).json()
        tree_map = {
            item["path"]: item["sha"]
            for item in tree.get("tree", [])
            if item.get("type") == "blob"
        }

        def read_file(rel_path: str) -> Optional[str]:
            sha = tree_map.get(rel_path)
            if not sha:
                return None
            resp = self._api(
                "GET", f"/repos/{self.cfg.owner}/{self.cfg.repo}/git/blobs/{sha}",
                accept="application/vnd.github.raw",
            )
            if resp is None:
                return None
            return resp.content.decode("utf-8")

        return RepoBase(head_sha, tree_sha, tree_map, read_file)

    def commit_files(self, base: RepoBase, files: Dict[str, bytes],
                     message: str) -> Optional[str]:
        """blob -> tree -> commit -> PATCH ref；无变化返回 None"""
        changed = {
            path: data for path, data in files.items()
            if git_blob_sha(data) != base.tree_map.get(path)
        }
        if not changed:
            return None

        tree_entries = []
        for rel_path, data in changed.items():
            blob = self._api(
                "POST", f"/repos/{self.cfg.owner}/{self.cfg.repo}/git/blobs",
                json={
                    "content": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64",
                },
            ).json()
            tree_entries.append({
                "path": rel_path,
                "mode": "100644",
                "type": "blob",
                "sha": blob["sha"],
            })

        tree = self._api(
            "POST", f"/repos/{self.cfg.owner}/{self.cfg.repo}/git/trees",
            json={"base_tree": base.tree_sha, "tree": tree_entries},
        ).json()

        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        author = {
            "name": self.cfg.author_name,
            "email": self.cfg.author_email,
            "date": now_iso,
        }
        commit = self._api(
            "POST", f"/repos/{self.cfg.owner}/{self.cfg.repo}/git/commits",
            json={
                "message": message,
                "tree": tree["sha"],
                "parents": [base.head_sha],
                "author": author,
                "committer": author,
            },
        ).json()

        self._api(
            "PATCH",
            f"/repos/{self.cfg.owner}/{self.cfg.repo}/git/refs/heads/{self.cfg.branch}",
            json={"sha": commit["sha"], "force": False},
        )
        return commit["sha"]


class PreviewWriter:
    """DRY_RUN 写入器：只写本地 data/archive_preview/，接口与 GitDataWriter 一致"""

    def __init__(self, cfg: ArchiveConfig):
        self.cfg = cfg
        self.root = Path(cfg.preview_dir)

    def check_private(self) -> None:
        return

    def _iter_files(self) -> Dict[str, str]:
        if not self.root.exists():
            return {}
        result = {}
        for path in self.root.rglob("*"):
            if path.is_file() and path.name != "_preview_commit.json":
                rel = path.relative_to(self.root).as_posix()
                result[rel] = git_blob_sha(path.read_bytes())
        return result

    def get_base(self) -> RepoBase:
        tree_map = self._iter_files()

        def read_file(rel_path: str) -> Optional[str]:
            path = self.root / rel_path
            if not path.exists():
                return None
            return path.read_text(encoding="utf-8")

        return RepoBase("preview", "preview", tree_map, read_file)

    def commit_files(self, base: RepoBase, files: Dict[str, bytes],
                     message: str) -> Optional[str]:
        changed = {
            path: data for path, data in files.items()
            if git_blob_sha(data) != base.tree_map.get(path)
        }
        if not changed:
            return None
        for rel_path, data in changed.items():
            path = self.root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        meta = {
            "message": message,
            "author": f"{self.cfg.author_name} <{self.cfg.author_email}>",
            "files": sorted(changed.keys()),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        (self.root / "_preview_commit.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(f"[DRY_RUN] 归档预览写入 {len(changed)} 个文件: {self.root}")
        return "preview"


# ============================================================
# 归档主类
# ============================================================

class EchoArchive:
    """队列 + 幂等 + 渲染 + 提交"""

    def __init__(self, cfg: ArchiveConfig, db):
        self.cfg = cfg
        self.db = db
        self.enabled = bool(cfg.enabled)
        self._queue: deque = deque()
        self._queued_ids: Set[str] = set()
        self._archived_ids: Set[str] = set()
        self._archived_order: List[str] = []
        self._state_loaded = False
        self._lock = asyncio.Lock()
        self._cursor_cache: Tuple[float, dict] = (0.0, {})
        self._warned_images: Set[str] = set()

        if not self.enabled:
            self.writer = None
        elif cfg.dry_run:
            self.writer = PreviewWriter(cfg)
        else:
            self.writer = GitDataWriter(cfg)
            self.writer.check_private()

    # --------------------------------------------------------
    # 入队 / 状态
    # --------------------------------------------------------

    async def enqueue(self, interaction: dict) -> None:
        """归档入队；立即返回，不阻塞邮件主流程"""
        if not self.enabled or not interaction:
            return
        rpid = str(interaction.get("comment_id") or "")
        if not rpid or rpid in self._queued_ids:
            return
        if rpid in self._archived_ids:
            # 同 rpid 不重复落盘；内容漂移以通知归档快照为准
            logger.debug(f"dup rpid {rpid}, defer to notify_archive")
            return
        if len(self._queue) >= self.cfg.max_queue:
            dropped = self._queue.popleft()
            self._queued_ids.discard(str(dropped.get("comment_id") or ""))
            logger.warning(f"归档队列已满，丢弃最老一条: {dropped.get('comment_id')}")
        self._queue.append(dict(interaction))
        self._queued_ids.add(rpid)

    async def _ensure_state_loaded(self) -> bool:
        """读取 comments/_state/archived_rpids.json；读失败则本轮不写，防止重复"""
        if self._state_loaded:
            return True
        try:
            base = await asyncio.to_thread(self.writer.get_base)
            raw = await asyncio.to_thread(base.read, COMMENTS_STATE_PATH)
            if raw:
                data = json.loads(raw)
                self._archived_order = [str(x) for x in data.get("rpids", [])][-5000:]
                self._archived_ids = set(self._archived_order)
            self._state_loaded = True
            return True
        except Exception as e:
            logger.warning(f"归档账本读取失败，本轮跳过: {e}")
            return False

    async def _get_cursor(self, base: RepoBase) -> dict:
        """读通知归档写的 pinned_cursor.json；内存缓存 5 分钟"""
        now = time.time()
        if now - self._cursor_cache[0] < 300:
            return self._cursor_cache[1]
        try:
            raw = await asyncio.to_thread(base.read, PINNED_CURSOR_PATH)
            cursor = json.loads(raw) if raw else {}
        except Exception as e:
            logger.warning(f"置顶游标读取失败，按空游标处理: {e}")
            cursor = {}
        self._cursor_cache = (now, cursor)
        return cursor

    async def _freeze_pinned(self, cursor: dict) -> None:
        """把当前/上一任置顶加入 never_archive，防止 168h 后级联清理"""
        if not self.cfg.never_archive_pinned:
            return
        pinned_rpid = str(cursor.get("pinned_rpid") or "")
        prev_rpid = str(cursor.get("prev_rpid") or "")
        pinned_dyn = str(cursor.get("pinned_dynamic_id") or "")
        if not pinned_rpid and not prev_rpid:
            return

        rows = []
        if pinned_rpid:
            item_id = pinned_dyn or await self.db.get_item_id_by_comment_id(pinned_rpid)
            if item_id:
                rows.append((item_id, pinned_rpid, "pinned", cursor.get("since") or ""))
        if prev_rpid:
            item_id = await self.db.get_item_id_by_comment_id(prev_rpid)
            if item_id:
                rows.append((item_id, prev_rpid, "prev_pinned", cursor.get("prev_since") or ""))

        for item_id, rpid, reason, since in rows:
            await self.db.add_never_archive(item_id, rpid, reason, since)
        if rows:
            logger.info(f"冻结 {len(rows)} 条历史置顶（never_archive）")

    # --------------------------------------------------------
    # flush
    # --------------------------------------------------------

    async def flush(self, reason: str = "timer") -> bool:
        """把队列写进归档仓；失败保留队列，下次重试"""
        if not self.enabled or not self._queue:
            return True
        if not await self._ensure_state_loaded():
            return False

        async with self._lock:
            pending = list(self._queue)
            if not pending:
                return True

            backoff = [0.8, 1.6, 3.2]
            for attempt in range(4):
                try:
                    base = await asyncio.to_thread(self.writer.get_base)
                    cursor = await self._get_cursor(base)
                    await self._freeze_pinned(cursor)
                    files, new_ids = await asyncio.to_thread(
                        self._build_files, base, pending, cursor
                    )
                    if not files:
                        self._mark_archived(pending)
                        return True
                    message = f"feat(archive): 归档 {len(pending)} 条互动（{reason}）"
                    commit = await asyncio.to_thread(
                        self.writer.commit_files, base, files, message
                    )
                    self._mark_archived(pending)
                    logger.info(
                        f"归档提交成功: {len(pending)} 条, commit={commit or '无变化'}"
                    )
                    return True
                except NonFastForward as e:
                    if attempt >= len(backoff):
                        logger.error(f"归档冲突重试耗尽: {e}")
                        return False
                    wait = backoff[attempt]
                    logger.warning(f"归档 ref 冲突，{wait}s 后重读重试: {e}")
                    await asyncio.sleep(wait)
                except Exception as e:
                    logger.error(f"归档失败，队列保留待重试: {e}")
                    return False
            return False

    def _mark_archived(self, pending: List[dict]) -> None:
        """提交成功后更新内存账本和队列"""
        for item in pending:
            rpid = str(item.get("comment_id") or "")
            if not rpid:
                continue
            if rpid not in self._archived_ids:
                self._archived_order.append(rpid)
            self._archived_ids.add(rpid)
            self._queued_ids.discard(rpid)
            try:
                self._queue.remove(item)
            except ValueError:
                pass
        if len(self._archived_order) > 5000:
            self._archived_order = self._archived_order[-5000:]
            self._archived_ids = set(self._archived_order)

    # --------------------------------------------------------
    # 构建文件
    # --------------------------------------------------------

    def _build_files(self, base: RepoBase, pending: List[dict],
                     cursor: dict) -> Tuple[Dict[str, bytes], Set[str]]:
        """在 base 快照上重建受影响的月文件，返回待提交文件"""
        pinned_rpid = str(cursor.get("pinned_rpid") or "")
        prev_rpid = str(cursor.get("prev_rpid") or "")
        pinned_dyn = str(cursor.get("pinned_dynamic_id") or "")
        pinned_since = str(cursor.get("since") or "")

        files: Dict[str, bytes] = {}
        emoji_assets: Dict[str, bytes] = {}
        new_ids: Set[str] = set()
        comments_by_month: Dict[str, List[dict]] = defaultdict(list)
        thread_by_day: Dict[str, List[dict]] = defaultdict(list)

        for item in pending:
            rpid = str(item.get("comment_id") or "")
            if not rpid or rpid in self._archived_ids:
                continue
            item_id = str(item.get("item_id") or "")
            day = _entry_day(item)
            month = day[:7]
            is_like = rpid.endswith("_like")
            route = "normal"

            if rpid == pinned_rpid:
                route = "pinned_body"
            elif rpid == prev_rpid:
                # 刚被换下的旧置顶优先于“同帖旁路”标记，保留“曾是置顶”语义
                route = "prev_pinned"
            elif pinned_rpid and item_id == pinned_dyn:
                # 游标刚切换、还没拿到 pinned_rpid 时先按普通互动处理，
                # 等 rpid 到位后由 _migrate_recent 搬到 thread，避免误判成回响
                route = "pinned_echo"

            rendered = self._render_entry(
                item, route, cursor, pinned_since,
                dest="thread" if route == "pinned_body" else "comments",
                base=base, assets=emoji_assets,
            )
            if route == "pinned_body":
                thread_by_day[day].append({
                    "kind": "body",
                    "rpid": rpid,
                    "md": rendered["md"],
                    "time": item.get("discovered_at", ""),
                })
                comments_by_month[month].append({
                    "rpid": rpid,
                    "pointer": True,
                    "relative": f"pinned/_thread/{day}.md",
                    "time": item.get("discovered_at", ""),
                    "csv": rendered["csv"],
                })
            else:
                comments_by_month[month].append({
                    "rpid": rpid,
                    "pointer": False,
                    "md": rendered["md"],
                    "csv": rendered["csv"],
                    "time": item.get("discovered_at", ""),
                })
                if route in ("pinned_echo", "prev_pinned"):
                    thread_by_day[day].append({
                        "kind": "echo",
                        "rpid": rpid,
                        "pinned_rpid": pinned_rpid,
                        "line": self._render_echo(item, route, cursor),
                        "time": item.get("discovered_at", ""),
                    })
            new_ids.add(rpid)

        # 把最近两个月 comments 里已是当前置顶的正文搬进 thread（切换竞态兜底）
        overrides = self._migrate_recent(
            base, pinned_rpid, comments_by_month, thread_by_day
        )

        # comments 月 MD + CSV（与空间归档一致：一个月一个文件）
        for month, entries in comments_by_month.items():
            md_path = f"comments/{month}.md"
            csv_path = f"comments/{month}.csv"
            existing_md = overrides.get(md_path) or base.read(md_path) or _comments_md_header(month)
            existing_csv = base.read(csv_path) or "﻿" + ",".join(CSV_FIELDS) + "\n"
            new_md, new_csv = _merge_comments(
                existing_md, existing_csv, entries, self.cfg, month
            )
            if new_md != existing_md:
                files[md_path] = new_md.encode("utf-8")
            if new_csv != existing_csv:
                files[csv_path] = new_csv.encode("utf-8")

        # thread 日文件
        for day, entries in thread_by_day.items():
            path = f"pinned/_thread/{day}.md"
            existing = base.read(path) or _thread_header(day)
            new_text = _merge_thread(existing, entries, day)
            if new_text != existing:
                files[path] = new_text.encode("utf-8")

        # 图片
        for item in pending:
            rpid = str(item.get("comment_id") or "")
            if not rpid or rpid in self._archived_ids or rpid.endswith("_like"):
                continue
            image = self._build_image(item)
            if image:
                path, data = image
                if base.tree_map.get(path) != git_blob_sha(data):
                    files[path] = data

        # 表情资源：和正文 MD 一起进同一次 commit
        files.update(emoji_assets)

        # comments README 索引
        readme_path = "comments/README.md"
        existing_readme = base.read(readme_path) or _comments_readme()
        new_readme = _update_comments_readme(existing_readme, comments_by_month.keys())
        if new_readme != existing_readme:
            files[readme_path] = new_readme.encode("utf-8")

        # 根 README 的「粉丝互动」月份索引：新互动归档后即时刷新
        root_readme_path = "README.md"
        existing_root = base.read(root_readme_path)
        if existing_root:
            months = _comment_months_from_index(new_readme)
            new_root = _update_root_comments_index(existing_root, months)
            if new_root != existing_root:
                files[root_readme_path] = new_root.encode("utf-8")

        # 幂等账本：只有真的有新 rpid 才写；否则 updated_at 会制造空 commit
        if new_ids:
            all_ids = list(dict.fromkeys(self._archived_order + sorted(new_ids)))[-5000:]
            state = json.dumps(
                {"rpids": all_ids, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                ensure_ascii=False, indent=2,
            )
            if base.read(COMMENTS_STATE_PATH) != state:
                files[COMMENTS_STATE_PATH] = state.encode("utf-8")

        return files, new_ids

    def _migrate_recent(self, base: RepoBase, pinned_rpid: str,
                        comments_by_month: Dict[str, List[dict]],
                        thread_by_day: Dict[str, List[dict]]) -> Dict[str, str]:
        """
        切换竞态兜底：扫描最近两个月 comments MD，发现某条 rpid 现在 == pinned_rpid，
        把正文搬到 thread，comments 只留指针。只处理还没被搬过的块。
        返回被改写的 comments MD 内容，供后续合并使用。
        """
        overrides: Dict[str, str] = {}
        if not pinned_rpid:
            return overrides
        current = datetime.now().strftime("%Y-%m")
        for month in (current, _prev_month(current)):
            md_path = f"comments/{month}.md"
            text = overrides.get(md_path) or base.read(md_path)
            if not text:
                continue
            moved = _extract_rpid_block(text, pinned_rpid)
            if not moved:
                continue
            body, new_text = moved
            overrides[md_path] = new_text
            day = _block_day(body) or datetime.now().strftime("%Y-%m-%d")
            thread_by_day[day].append({
                "kind": "body",
                "rpid": pinned_rpid,
                "md": body,
                "time": "",
            })
            comments_by_month.setdefault(month, []).append({
                "rpid": pinned_rpid,
                "pointer": True,
                "relative": f"pinned/_thread/{day}.md",
                "time": "",
            })
        return overrides

    def _build_image(self, item: dict) -> Optional[Tuple[str, bytes]]:
        """把 sent_emails/dynamic_{item_id}.jpeg 转成 {rpid}.webp"""
        rpid = str(item.get("comment_id") or "")
        item_id = str(item.get("item_id") or "")
        if not rpid or not item_id or rpid.endswith("_like"):
            return None
        src = Path(self.cfg.screenshot_dir) / f"dynamic_{item_id}.jpeg"
        if not src.exists():
            if item_id not in self._warned_images:
                self._warned_images.add(item_id)
                logger.warning(f"原帖截图不存在，降级纯文本: {src}")
            return None
        raw = src.read_bytes()
        webp = _to_webp(raw, self.cfg.image_max_size, self.cfg.webp_quality)
        if not webp:
            return None
        day = _entry_day(item)
        path = f"comments/assets/{day[:4]}/{day[5:7]}/{rpid}.webp"
        return path, webp

    # --------------------------------------------------------
    # 渲染
    # --------------------------------------------------------

    def _render_entry(self, item: dict, route: str, cursor: dict,
                      pinned_since: str, dest: str = "comments",
                      base: Optional[RepoBase] = None,
                      assets: Optional[Dict[str, bytes]] = None) -> Dict[str, str]:
        """单条互动 -> MD 正文 + CSV 行"""
        rpid = str(item.get("comment_id") or "")
        item_id = str(item.get("item_id") or "")
        item_type = str(item.get("item_type") or "")
        scene = str(item.get("scene") or "")
        scene_label = SCENE_LABELS.get(scene, scene)
        is_sub = bool(item.get("is_sub_reply"))
        up_liked = bool(item.get("up_liked"))
        priority = 1 if (rpid == str(cursor.get("pinned_rpid") or "")
                         or item_id == str(cursor.get("pinned_dynamic_id") or "")) else 0
        url = build_url(item_id, item_type, item.get("comment_oid", ""), rpid)

        tags = [f"`{scene_label}`"]
        if is_sub:
            tags.append("`楼中楼`")
        if up_liked:
            tags.append("`UP觉得很赞`")
        if priority:
            tags.append("`优先`")
        if route == "prev_pinned":
            prev_since = str(cursor.get("prev_since") or "")
            tags.append(f"`曾是置顶({esc_md(prev_since)}起)`")

        lines = [f"<!-- ew:rpid:{rpid} -->"]
        lines.append(f"### {esc_md(item.get('discovered_at', ''))} · {' '.join(tags)}")
        lines.append("")

        parent_author = item.get("parent_author") or ""
        parent_content = item.get("parent_content") or ""
        if is_sub and parent_author:
            parent_text = flatten_content(parent_content, item.get("parent_rich_content"))
            if len(parent_text) > 160:
                parent_text = parent_text[:160] + "…"
            lines.append(f"> 回 @{esc_nick(parent_author, self.cfg)}：{parent_text}")
            lines.append("")

        content_text = flatten_content(item.get("content"), item.get("rich_content"))
        content_md = self._render_content_md(
            item, dest, base, assets, "content", "rich_content"
        )
        author = esc_nick(item.get("comment_author") or "", self.cfg)
        if up_liked and not is_sub:
            lines.append(f"**{author or 'UP主'}点赞了**：{content_md}")
        else:
            lines.append(f"**{author or 'UP主'}说**：{content_md}")
        lines.append("")

        image_path = self._image_repo_path(item)
        if image_path:
            lines.append(
                f"![原帖截图]({_relative_asset(dest, _entry_day(item), image_path)})"
            )
            lines.append("")

        lines.append(f"- 原帖：`{esc_md(item_id)}`　rpid：`{esc_md(rpid)}`")
        lines.append(f"- 直达：{url}")
        lines.append("")
        md = "\n".join(lines)

        row = {
            "discovered_at": item.get("discovered_at", ""),
            "scene": scene,
            "scene_label": scene_label,
            "rpid": rpid,
            "is_sub": "1" if is_sub else "0",
            "up_liked": "1" if up_liked else "0",
            "priority": str(priority),
            "item_id": item_id,
            "item_type": item_type,
            "parent_author": esc_nick(parent_author, self.cfg),
            "parent_content": flatten_content(parent_content, item.get("parent_rich_content"))[:160],
            "content": content_text,
            "has_image": "1" if image_path else "0",
            "url": url,
        }
        return {"md": md, "csv": row}

    def _render_content_md(self, item: dict, dest: str,
                           base: Optional[RepoBase],
                           assets: Optional[Dict[str, bytes]],
                           text_key: str, rich_key: str) -> str:
        """正文 MD：表情 [xx] 换成仓内图片，其余按 Markdown 转义"""
        text = item.get(text_key) or ""
        try:
            rich = json.loads(item.get(rich_key) or "{}")
        except (TypeError, ValueError):
            rich = {}

        name_to_path: Dict[str, str] = {}
        for key, info in (rich.get("emote") or {}).items():
            if not isinstance(info, dict):
                continue
            url = (info.get("url") or "").strip()
            if not url:
                continue
            repo_path = _emoji_repo_path(url)
            exists = bool(assets and repo_path in assets)
            if not exists and base:
                exists = bool(base.tree_map.get(repo_path))
            if not exists:
                data = self._download_emoji(url)
                if not data:
                    continue
                if assets is not None:
                    assets[repo_path] = data
            name_to_path[key.strip("[]")] = _relative_asset(
                dest, _entry_day(item), repo_path
            )

        parts = re.split(r"(\[[^\]]+\])", text)
        output = []
        for part in parts:
            if part.startswith("[") and part.endswith("]"):
                name = part[1:-1]
                path = name_to_path.get(name)
                if path:
                    output.append(f"![{esc_md(name)}]({path})")
                    continue
            output.append(esc_md(part))

        pictures = rich.get("pictures") or []
        if pictures:
            output.append(" " + " ".join(["[图片]"] * len(pictures)))
        return "".join(output)

    def _download_emoji(self, url: str) -> Optional[bytes]:
        """下载并转 WebP；失败返回 None，正文保留 [xx] 文本"""
        try:
            resp = requests.get(
                url, timeout=self.cfg.timeout,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            data = _to_webp(resp.content, 256, self.cfg.webp_quality)
            if data is None:
                logger.warning(f"表情转 WebP 失败: {url[:80]}")
            return data
        except Exception as e:
            logger.warning(f"表情下载失败 {url[:80]}: {e}")
            return None

    def _render_echo(self, item: dict, route: str, cursor: dict) -> str:
        """thread 文件里的一行“回响”"""
        rpid = str(item.get("comment_id") or "")
        item_id = str(item.get("item_id") or "")
        item_type = str(item.get("item_type") or "")
        content = flatten_content(item.get("content"), item.get("rich_content"))
        if len(content) > 80:
            content = content[:80] + "…"
        url = build_url(item_id, item_type, item.get("comment_oid", ""), rpid)
        when = str(item.get("discovered_at") or "")[5:16]
        tag = ""
        if item.get("up_liked"):
            tag = " `UP觉得很赞`"
        elif item.get("is_sub_reply"):
            tag = " `楼中楼`"
        if item.get("is_sub_reply") and item.get("parent_author"):
            content = f"回 @{esc_nick(item.get('parent_author'), self.cfg)}：{content}"
        elif item.get("up_liked"):
            content = f"点赞了 @{esc_nick(item.get('comment_author'), self.cfg)}：{content}"
        return f"- {esc_md(when)} {content} → [原帖]({url}){tag} <!-- ew:echo:{rpid} -->"

    def _image_repo_path(self, item: dict) -> Optional[str]:
        rpid = str(item.get("comment_id") or "")
        item_id = str(item.get("item_id") or "")
        if not PIL_AVAILABLE or not rpid or not item_id or rpid.endswith("_like"):
            return None
        src = Path(self.cfg.screenshot_dir) / f"dynamic_{item_id}.jpeg"
        if not src.exists():
            return None
        day = _entry_day(item)
        return f"comments/assets/{day[:4]}/{day[5:7]}/{rpid}.webp"

    # --------------------------------------------------------
    # 手动补档
    # --------------------------------------------------------

    async def backfill_notified(self, hours: int = 48, limit: int = 500) -> int:
        """把最近 N 小时已通知但未归档的互动重新入队（不发邮件）"""
        rows = await self.db.get_notified_since(hours, limit)
        count = 0
        for row in rows:
            rpid = str(row.get("comment_id") or "")
            if rpid and rpid not in self._archived_ids:
                await self.enqueue(row)
                count += 1
        logger.info(f"手动补档入队: {count} 条（最近 {hours}h）")
        return count


# ============================================================
# 文件渲染 / 合并工具
# ============================================================

def _entry_day(item: dict) -> str:
    """从 discovered_at 取日期；异常时用今天"""
    raw = str(item.get("discovered_at") or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _prev_month(month: str) -> str:
    """YYYY-MM 的上一个月"""
    year, mon = (int(x) for x in month.split("-"))
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def _block_day(block: str) -> str:
    """从事件块标题里取日期"""
    match = re.search(r"^### (\d{4}-\d{2}-\d{2})", block, re.M)
    return match.group(1) if match else ""


def _comments_md_header(month: str) -> str:
    """与空间归档一致的月文件头（表格由 _merge_comments 重建）"""
    return (
        "<!-- ARCHIVE_HEADER_START -->\n"
        f"# EchoWatch 互动归档 · {month}\n\n"
        "> 本月共 0 条\n\n"
        "| 时间 | 类别 | 摘要 |\n"
        "| --- | --- | --- |\n"
        "<!-- ARCHIVE_HEADER_END -->\n\n"
        "<!-- ARCHIVE_SECTIONS_START -->\n"
        "<!-- ARCHIVE_SECTIONS_END -->\n"
    )


def _thread_header(day: str) -> str:
    return f"# 置顶回响 · {day}\n\n"


def _comments_readme() -> str:
    return (
        "# EchoWatch 互动归档\n\n"
        "本目录由 echowatch-bot 自动维护，按月追加。\n\n"
        f"{INDEX_ANCHOR}\n{INDEX_ANCHOR}\n"
    )


def _emoji_repo_path(url: str) -> str:
    """表情 URL -> 仓内固定路径（与通知归档的 assets/emoji 命名同构）"""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"comments/assets/emoji/{digest}.webp"


def _relative_asset(kind: str, day: str, repo_path: str) -> str:
    """
    把仓内路径转成相对当前 MD 的链接。
    comments/YYYY-MM.md -> assets/YYYY/MM/rpid.webp
    pinned/_thread/YYYY-MM-DD.md -> ../../comments/assets/YYYY/MM/rpid.webp
    """
    if repo_path.startswith("comments/assets/"):
        rest = repo_path[len("comments/assets/"):]
        if kind == "comments":
            return f"assets/{rest}"
        return f"../../comments/assets/{rest}"
    return repo_path


def _split_md_sections(md: str) -> List[str]:
    """拆出月 MD 里的事件块（rpid / pointer）"""
    start = md.find(MD_SECTIONS_START)
    end = md.find(MD_SECTIONS_END)
    if start == -1 or end == -1 or end < start:
        return []
    body = md[start + len(MD_SECTIONS_START):end].strip()
    return [
        block.strip() for block in
        re.split(r"(?=<!-- ew:(?:rpid|pointer):)", body)
        if block.strip()
    ]


def _block_key(block: str) -> Tuple[str, str]:
    """返回（rpid、类型 rpid/pointer）"""
    match = re.search(r"<!-- ew:(rpid|pointer):([^\s]+) -->", block)
    return (match.group(2), match.group(1)) if match else ("", "")


def _table_cell(text: str) -> str:
    """表格单元格单行化并转义竖线"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text.replace("|", "\\|")


def _clip(text: str, limit: int = 40) -> str:
    """摘要截断，和其他归档分组的表头一致"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _merge_comments(existing_md: str, existing_csv: str,
                    entries: List[dict], cfg: ArchiveConfig,
                    month: str) -> Tuple[str, str]:
    """把新条目合并进 comments 月 MD/CSV，按 rpid 去重，重建表头"""
    rows = list(csv.DictReader(io.StringIO(existing_csv.lstrip("﻿")))) if existing_csv.strip() else []
    existing_ids = {r.get("rpid") for r in rows if r.get("rpid")}
    blocks = _split_md_sections(existing_md)
    block_ids = {_block_key(block)[0] for block in blocks if _block_key(block)[0]}

    for entry in entries:
        rpid = entry.get("rpid", "")
        if not rpid:
            continue
        if entry.get("pointer"):
            pointer_text = f"↗ 已并入 {entry.get('relative', 'pinned/_thread/')}"
            if rpid not in block_ids:
                blocks.append(
                    f"<!-- ew:pointer:{rpid} -->\n"
                    f"> {pointer_text}（本条即当期置顶评论本体）"
                )
                block_ids.add(rpid)
            if rpid not in existing_ids:
                row = {k: "" for k in CSV_FIELDS}
                row.update(entry.get("csv") or {})
                row.update({
                    "discovered_at": entry.get("time", "") or row.get("discovered_at", ""),
                    "rpid": rpid,
                    "content": pointer_text,
                    "has_image": "0",
                })
                rows.append(row)
                existing_ids.add(rpid)
            else:
                # 切换竞态搬移时，CSV 里原来的正文也要改成指针，避免正文双份
                for row in rows:
                    if row.get("rpid") == rpid:
                        row["content"] = pointer_text
                        row["has_image"] = "0"
                        break
        else:
            if rpid not in block_ids:
                blocks.append(entry.get("md", "").strip())
                block_ids.add(rpid)
            if rpid not in existing_ids:
                rows.append(entry.get("csv") or {})
                existing_ids.add(rpid)

    rows.sort(key=lambda r: r.get("discovered_at", ""))
    row_time = {r.get("rpid"): r.get("discovered_at", "") for r in rows}

    def block_sort_time(block: str) -> str:
        rpid, kind = _block_key(block)
        if kind == "pointer":
            return row_time.get(rpid, "")
        heading = re.search(r"^### (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", block, re.M)
        return heading.group(1) if heading else row_time.get(rpid, "")

    blocks.sort(key=block_sort_time)

    last = rows[-1].get("discovered_at", "") if rows else ""
    header = [
        f"# EchoWatch 互动归档 · {month}",
        "",
        f"> 本月共 {len(rows)} 条 · 最后更新 {last}",
        "",
        "| 时间 | 类别 | 摘要 |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        header.append(
            f"| {_table_cell(row.get('discovered_at', ''))} "
            f"| {_table_cell(row.get('scene_label', ''))} "
            f"| {_table_cell(_clip(row.get('content', '')))} |"
        )

    md = (
        f"{MD_HEADER_START}\n" + "\n".join(header) + f"\n{MD_HEADER_END}\n\n"
        f"{MD_SECTIONS_START}\n" + "\n\n".join(blocks) + f"\n\n{MD_SECTIONS_END}\n"
    )

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    return md, "﻿" + out.getvalue()


def _extract_rpid_block(text: str, rpid: str) -> Optional[Tuple[str, str]]:
    """从 comments MD 里取出某个 rpid 的正文块，并把原位置换成指针"""
    marker = f"<!-- ew:rpid:{rpid} -->"
    start = text.find(marker)
    if start == -1:
        return None
    nxt = re.search(r"<!-- ew:(?:rpid|pointer):", text[start + len(marker):])
    end = start + len(marker) + nxt.start() if nxt else len(text)
    while end > start and text[end - 1] in "\r\n":
        end -= 1
    body = text[start:end].strip()
    pointer = (
        f"<!-- ew:pointer:{rpid} -->\n"
        f"> ↗ 已并入 pinned/_thread/（本条即当期置顶评论本体）\n"
    )
    return body, (text[:start] + pointer + text[end:])


def _merge_thread(existing: str, entries: List[dict], day: str) -> str:
    """把 body/echo 合并进 thread 日的 echowatch:thread 块"""
    block_match = re.search(
        re.escape(THREAD_ANCHOR) + r"(.*?)" + re.escape(THREAD_ANCHOR),
        existing, re.S,
    )
    block = block_match.group(1).strip() if block_match else ""
    prefix = existing[:block_match.start()] if block_match else existing.rstrip()
    suffix = existing[block_match.end():] if block_match else ""

    # 解析已有 section：### 置顶 rpid x
    sections: Dict[str, dict] = {}
    for sec in re.split(r"(?=^#{2,3} 置顶 rpid )", block, flags=re.M):
        m = re.match(r"#{2,3} 置顶 rpid ([^\s]+)", sec)
        if not m:
            continue
        rpid = m.group(1)
        body_part = ""
        echoes: List[str] = []
        if "#### 回响" in sec:
            body_part, echo_part = sec.split("#### 回响", 1)
            echoes = [line.strip() for line in echo_part.splitlines()
                      if line.strip().startswith("- ")]
        else:
            body_part = sec
        body_part = re.sub(r"^#{2,3} 置顶 rpid [^\n]+\n?", "", body_part).strip()
        sections[rpid] = {"body": body_part, "echoes": echoes}

    for entry in entries:
        if entry.get("kind") == "body":
            rpid = entry.get("rpid", "")
            sec = sections.setdefault(rpid, {"body": "", "echoes": []})
            sec["body"] = entry.get("md", "")
        elif entry.get("kind") == "echo":
            target = entry.get("pinned_rpid") or "unknown"
            sec = sections.setdefault(target, {"body": "", "echoes": []})
            line = entry.get("line", "")
            key = re.search(r"<!-- ew:echo:([^\s]+) -->", line)
            if key and not any(key.group(1) in x for x in sec["echoes"]):
                sec["echoes"].append(line)

    parts = []
    for rpid, sec in sections.items():
        if not sec["body"] and not sec["echoes"]:
            continue
        # 同一 rpid 如果已经是置顶本体，就不再保留它的“回响”行
        if f"<!-- ew:rpid:{rpid} -->" in sec["body"]:
            sec["echoes"] = [
                line for line in sec["echoes"]
                if f"<!-- ew:echo:{rpid} -->" not in line
            ]
        parts.append(f"## 置顶 rpid {rpid}\n")
        if sec["body"]:
            parts.append(sec["body"] + "\n")
        if sec["echoes"]:
            parts.append("#### 回响\n")
            parts.extend(line + "\n" for line in sec["echoes"])
        parts.append("")
    parts.append(f"_同步于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} by echowatch-bot_")
    new_block = "\n".join(parts).strip()

    if block_match:
        return (prefix + THREAD_ANCHOR + "\n" + new_block + "\n" + THREAD_ANCHOR + suffix)
    return prefix.rstrip() + "\n\n" + THREAD_ANCHOR + "\n" + new_block + "\n" + THREAD_ANCHOR + "\n"


def _update_comments_readme(existing: str, months) -> str:
    """维护 comments/README.md 的月份索引；保留已存在的月份条目"""
    existing_months = set(
        re.findall(r"- \[(\d{4}-\d{2})\]\((?:\./)?\1\.md\)", existing or "")
    )
    months = sorted(existing_months | set(months), reverse=True)
    lines = ["# EchoWatch 互动归档", "", "本目录由 echowatch-bot 自动维护，按月追加。", ""]
    lines.append(INDEX_ANCHOR)
    for month in months:
        lines.append(
            f"- [{month}](./{month}.md) · [CSV](./{month}.csv)"
        )
    lines.append(INDEX_ANCHOR)
    return "\n".join(lines) + "\n"


def _comment_months_from_index(readme: str) -> List[str]:
    """从 comments/README.md 的月份索引提取月份"""
    months = set()
    for match in re.finditer(r"- \[(\d{4}-\d{2})\]\((?:\./)?\1\.md\)", readme or ""):
        months.add(match.group(1))
    return sorted(months, reverse=True)


def _update_root_comments_index(readme: str, months: List[str]) -> str:
    """只更新根 README 归档索引里的「粉丝互动」月份，其他分类原样保留"""
    start = readme.find(ROOT_INDEX_START)
    end = readme.find(ROOT_INDEX_END)
    if start == -1 or end == -1 or end < start:
        return readme

    block_start = start + len(ROOT_INDEX_START)
    block = readme[block_start:end]
    pos = block.find(COMMENTS_SECTION)
    if pos == -1:
        return readme

    next_pos = block.find("\n### ", pos + len(COMMENTS_SECTION))
    if next_pos == -1:
        next_pos = len(block)

    lines = [COMMENTS_SECTION]
    for item in months:
        lines.append(
            f"- [{item}](comments/{item}.md) · [CSV](comments/{item}.csv)"
        )
    lines.append("- [EchoWatch 互动归档](comments/README.md)")
    section = "\n".join(lines) + "\n"
    # 章节在索引块末尾时补一个空行，保持和其他分类一致的排版
    if next_pos == len(block):
        section += "\n"

    new_block = block[:pos] + section + block[next_pos:]
    return readme[:block_start] + new_block + readme[end:]


# ============================================================
# 手动补档 CLI
# ============================================================

async def _cli_backfill(hours: int, limit: int) -> None:
    """命令行：python echowatch_archive.py --backfill-hours 48 --limit 500"""
    from config import Config
    from database import Database

    config = Config("config.yaml")
    db = await Database(config.database.path).initialize()
    try:
        cfg = ArchiveConfig.load(config.github_archive)
        arch = EchoArchive(cfg, db)
        if not arch.enabled:
            print("归档未启用，请先在 config_github.py 或 config.yaml 打开")
            return
        await arch.backfill_notified(hours, limit)
        await arch.flush("manual_backfill")
    finally:
        await db.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill-hours", type=int, default=48)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    asyncio.run(_cli_backfill(args.backfill_hours, args.limit))
