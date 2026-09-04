# bili_client.py
"""EchoWatch B站公开API客户端 —— 纯HTTP请求+WBI签名，无需Cookie，无需bilibili-api-python"""

import asyncio
import hashlib
import json
import time
import urllib.parse
from typing import Optional

import aiohttp

from logger_config import logger
from retry_decorator import API_RETRY, async_retry


# ============================================================
# 常量
# ============================================================

# B站公开API端点
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
MAIN_COMMENT_URL = "https://api.bilibili.com/x/v2/reply/wbi/main"
SUB_COMMENT_URL = "https://api.bilibili.com/x/v2/reply/reply"
SPACE_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
TOPIC_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/topic"
USER_INFO_URL = "https://api.bilibili.com/x/web-interface/card"

# WBI固定混淆表（B站前端硬编码，不会变）
MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 动态类型 → 评论区类型（整数，直接对应B站API的comment_type参数）
DYNAMIC_TYPE_TO_COMMENT = {
    "DYNAMIC_TYPE_AV": 1,           # 视频
    "DYNAMIC_TYPE_DRAW": 11,        # 图文动态
    "DYNAMIC_TYPE_ARTICLE": 12,     # 专栏
    "DYNAMIC_TYPE_WORD": 17,        # 纯文字动态
    "DYNAMIC_TYPE_FORWARD": 17,     # 转发动态 → 按文字动态
    "DYNAMIC_TYPE_LIVE_RCMD": 1,    # 直播回放 → 按视频
}
DEFAULT_COMMENT_TYPE = 11  # 默认按图文动态处理

# 请求头（模拟正常浏览器，不声明br避免brotli依赖）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com/",
}

# WBI密钥缓存时长（秒）
MIXIN_KEY_TTL = 3600
# 动态列表缓存有效期（秒）：风控期间用最近一次成功结果兜底，超时视为过期
DYN_CACHE_MAX_AGE = 3600
RATELIMIT_BREAKER_HITS = 3   # 连续N次风控命中（-352/-412）触发熔断，冷却期内不再请求空间动态接口
# buvid3 获取失败后的重试节流间隔（秒）：避免每次API调用都打B站首页
BUVID_RETRY_INTERVAL = 60
# 子评论每页条数（与B站前端一致，ps=10 比 20 更稳，实测可完整翻页）
SUB_COMMENT_PAGE_SIZE = 10


# ============================================================
# WBI 签名（纯函数，方便测试）
# ============================================================

def get_mixin_key_from_urls(img_url: str, sub_url: str) -> str:
    """
    从nav接口返回的两个图片URL提取文件名拼接后，按混淆表生成mixinKey。

    Args:
        img_url: nav返回的 img_url 字段
        sub_url: nav返回的 sub_url 字段

    Returns:
        32位的 mixinKey 字符串
    """
    img_key = img_url.split("/")[-1].split(".")[0]
    sub_key = sub_url.split("/")[-1].split(".")[0]
    origin = img_key + sub_key
    return "".join(origin[n] for n in MIXIN_TABLE)[:32]


def wbi_sign_params(params: dict, mixin_key: str) -> dict:
    """
    对请求参数做WBI签名，追加w_rid和wts。
    算法：参数字典序排序 → URL query string → 拼mixinKey → MD5

    Args:
        params: 原始请求参数（含wts，不含w_rid）
        mixin_key: 32位的WBI签名密钥

    Returns:
        追加了w_rid的参数字典（新字典，不修改入参）
    """
    # wts 必须参与签名（与B站前端逻辑一致）
    params_with_ts = {**params, "wts": int(time.time())}
    # 按key字典序排序，encodeURIComponent风格编码
    sorted_items = sorted(params_with_ts.items())
    query = urllib.parse.urlencode(sorted_items, quote_via=urllib.parse.quote)
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return {**params_with_ts, "w_rid": w_rid}


# ============================================================
# 评论数据解析（纯函数，方便测试）
# ============================================================

def parse_comment(raw: dict) -> dict:
    """
    从B站评论API原始数据中提取关键字段。

    Args:
        raw: API返回的单条评论dict

    Returns:
        {rpid, mid, uname, content, rich_content, ctime, up_action_like, replies_count}
        rich_content 是 content 对象中 emote/pictures/jump_url 的 JSON 字符串
    """
    member = raw.get("member", {})
    content = raw.get("content", {})
    up_action = raw.get("up_action", {})
    # 提取富内容（表情映射、评论图片、超链接），序列化为 JSON 供下游渲染
    rich_content = json.dumps({
        "emote": content.get("emote", {}),
        "pictures": content.get("pictures", []),
        "jump_url": content.get("jump_url", {}),
    }, ensure_ascii=False)

    return {
        "rpid": raw.get("rpid"),
        "mid": str(member.get("mid", "")),
        "uname": member.get("uname", ""),
        "content": content.get("message", ""),
        "rich_content": rich_content,
        "ctime": raw.get("ctime", 0),
        "up_action_like": up_action.get("like", False),
        "replies_count": raw.get("rcount", 0),
    }


def _parse_space_feed(data: dict) -> list[dict]:
    """
    解析polymer空间动态API响应，提取评论轮询所需字段。
    模块级函数，方便BiliClient和测试共用。
    """
    items = data.get("items", [])
    result = []
    for item in items:
        dyn_id = item.get("id_str", "")
        dyn_type = item.get("type", "")
        basic = item.get("basic", {})
        comment_oid = str(basic.get("comment_id_str", ""))

        modules = item.get("modules", {})
        mod_dyn = modules.get("module_dynamic", {})
        desc = mod_dyn.get("desc")
        content_text = desc.get("text", "") if isinstance(desc, dict) else ""

        # 视频类型优先用 aid（视频评论区），其他类型用 comment_id_str
        major = mod_dyn.get("major") or {}
        archive = major.get("archive") or {}
        aid = str(archive.get("aid", ""))
        if dyn_type == "DYNAMIC_TYPE_AV" and aid:
            comment_oid = aid
        if not comment_oid:
            comment_oid = dyn_id

        mod_author = modules.get("module_author") or {}
        pub_ts = mod_author.get("pub_ts", 0)

        result.append({
            "dynamic_id": dyn_id,
            "item_id": dyn_id,
            "comment_oid": comment_oid,
            "type": dyn_type,
            "content": content_text.strip(),
            "pub_ts": pub_ts,
        })
    return result


def _extract_text_from_rich_nodes(rich_nodes: list) -> str:
    """
    从 rich_text_nodes 中提取纯文本，用于 desc.text 为空时的降级。

    B站 polymer API 中部分动态的 desc.text 为空，但 desc.rich_text_nodes
    包含了结构化的文本节点。本函数按顺序拼接各节点的文字内容：

    - RICH_TEXT_NODE_TYPE_TEXT   → 直接取 text 字段（纯文本）
    - RICH_TEXT_NODE_TYPE_EMOJI  → 取 emoji.text（如 "[微笑]"）
    - RICH_TEXT_NODE_TYPE_AT     → 取 text 字段（如 "@XXX"）
    - RICH_TEXT_NODE_TYPE_LINK   → 取 text 字段（链接文字）
    - RICH_TEXT_NODE_TYPE_TOPIC  → 取 text 字段（话题标签）

    Returns:
        拼接后的纯文本字符串，无内容则返回 ""
    """
    parts = []
    for node in (rich_nodes or []):
        node_type = node.get("type", "")
        if node_type == "RICH_TEXT_NODE_TYPE_TEXT":
            parts.append(node.get("text", ""))
        elif node_type == "RICH_TEXT_NODE_TYPE_EMOJI":
            emoji_data = node.get("emoji", {})
            parts.append(emoji_data.get("text", ""))
        elif node_type in ("RICH_TEXT_NODE_TYPE_AT", "RICH_TEXT_NODE_TYPE_LINK",
                           "RICH_TEXT_NODE_TYPE_TOPIC"):
            parts.append(node.get("text", ""))
        # 其他类型（如 WEB_LINK）不处理，避免引入非文本噪音
    return "".join(parts)


def _extract_post_rich_content(dci: dict) -> str:
    """
    从话题动态卡片中提取富内容，序列化为与 render_comment_html 兼容的 JSON。

    提取内容：
    - 表情：rich_text_nodes 中 type=EMOJI 的节点，收集 emoji.text → emoji.icon_url
    - 图片：major.draw.items 的各张图片 src
    - 视频：major.archive 的标题/封面/ID

    Returns:
        JSON 字符串，结构为 {"emote": {...}, "pictures": [...], "jump_url": {}, "video": {...}}
        空内容返回 "{}"
    """
    modules = dci.get("modules", {})
    mod_dyn = modules.get("module_dynamic") or {}
    major = mod_dyn.get("major") or {}
    desc = mod_dyn.get("desc") or {}

    # 提取表情（rich_text_nodes → emote 映射）
    emotes = {}
    rich_nodes = desc.get("rich_text_nodes") or []
    for node in rich_nodes:
        if node.get("type") == "RICH_TEXT_NODE_TYPE_EMOJI":
            emoji_data = node.get("emoji", {})
            text = emoji_data.get("text", "")
            icon_url = emoji_data.get("icon_url", "")
            if text and icon_url:
                emotes[text] = {"url": icon_url, "meta": {"size": 1}}

    # 提取图片（major.draw.items）
    pictures = []
    draw = major.get("draw") or {}
    for item in draw.get("items", []):
        src = item.get("src", "")
        if src:
            pictures.append({"img_src": src})

    # 提取视频信息
    video_info = {}
    archive = major.get("archive") or {}
    if archive.get("title"):
        video_info = {
            "title": archive.get("title", ""),
            "cover": archive.get("cover", ""),
            "aid": str(archive.get("aid", "")),
            "bvid": archive.get("bvid", ""),
        }

    return json.dumps({
        "emote": emotes,
        "pictures": pictures,
        "jump_url": {},
        "video": video_info,
    }, ensure_ascii=False)


def parse_sub_comment(raw: dict) -> dict:
    """
    从B站子评论API原始数据中提取关键字段。

    Args:
        raw: API返回的单条子评论dict

    Returns:
        {rpid, mid, uname, content, rich_content, ctime, parent_rpid, up_action_like}
    """
    member = raw.get("member", {})
    content = raw.get("content", {})
    up_action = raw.get("up_action", {})

    # 防御：子评论 content 可能不是 dict
    if isinstance(content, dict):
        message = content.get("message", "")
        rich_content = json.dumps({
            "emote": content.get("emote", {}),
            "pictures": content.get("pictures", []),
            "jump_url": content.get("jump_url", {}),
        }, ensure_ascii=False)
    else:
        message = ""
        rich_content = "{}"

    return {
        "rpid": raw.get("rpid"),
        "mid": str(member.get("mid", "")),
        "uname": member.get("uname", ""),
        "content": message,
        "rich_content": rich_content,
        "ctime": raw.get("ctime", 0),
        "parent_rpid": raw.get("parent", 0),
        "up_action_like": up_action.get("like", False),
    }


# ============================================================
# BiliClient 类
# ============================================================

class BiliClient:
    """
    B站公开API客户端 —— 纯HTTP请求 + WBI签名，核心路径零Cookie。

    职责:
    - WBI签名管理（密钥自动缓存刷新）
    - 评论获取（一级+子评论，游标翻页）
    - 用户/话题动态列表发现
    - 自动获取buvid3（匿名设备指纹），仅用于空间动态API
    """

    def __init__(self, breaker_cooldown_seconds: int = 1800, comment_direct: bool = False):
        """
        初始化客户端，HTTP会话延迟创建。

        breaker_cooldown_seconds: 风控熔断冷却时长（秒，来自 config.breaker.ratelimit_cooldown_seconds）
        comment_direct: True=评论/子评论扫描直连（不读代理环境变量）——
            评论接口对IP不敏感（space feed被-412时评论照常应答），直连可让主功能
            不受机场/代理故障连累（2026-08-28 新增，观察期验证）
        """
        self._comment_direct = comment_direct
        logger.info(f"评论接口通道: {'直连(不代理)' if comment_direct else '代理'}")
        self._session: Optional[aiohttp.ClientSession] = None
        self._sub_session: Optional[aiohttp.ClientSession] = None  # 子评论翻页专用会话（独立CookieJar）
        self._mixin_key: Optional[str] = None
        self._mixin_key_ts: float = 0
        self._buvid_ready: bool = False
        self._buvid_failed_at: float = 0.0  # 上次获取buvid失败时间（节流重试用）
        self._session_lock = asyncio.Lock()
        self._topic_sort_by_cache: dict[int, int] = {}  # topic_id → "最新"排序值
        self._dyn_cache: dict[str, tuple] = {}  # uid → (时间戳, 动态列表)，风控兜底用
        # 风控熔断状态（2026-08-27 新增）：连续命中 -352/-412 触发冷却，期间只走缓存兜底不发请求
        self._breaker_cooldown_seconds = breaker_cooldown_seconds
        self._rl_hit_count = 0            # 连续风控命中计数
        self._rl_cooldown_until = 0.0     # 熔断冷却截止时间戳（0=未在熔断）

    # ----------------------------------------------------------
    # 内部：会话与签名
    # ----------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """延迟创建aiohttp会话（首次API调用时），启用CookieJar用于存储buvid"""
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    jar = aiohttp.CookieJar()
                    connector = aiohttp.TCPConnector(limit=10)
                    timeout = aiohttp.ClientTimeout(total=30)
                    self._session = aiohttp.ClientSession(
                        headers=HEADERS, connector=connector, timeout=timeout, cookie_jar=jar,
                        trust_env=True
                    )
        return self._session

    async def _get_sub_session(self) -> aiohttp.ClientSession:
        """
        子评论翻页专用会话：独立CookieJar，永不种buvid。

        实测（2026-08-14 服务器A/B复现）：会话带上真实buvid3+b_nut后，
        x/v2/reply/reply 从第2页起静默返回空列表（code=0不报错），
        翻页只拿得到第1页 → UP回复（通常靠后）永久漏检。
        干净会话翻页正常（与Freeview插件 credentials:omit 不带cookie一致）。
        主会话仍负责种buvid（space feed/topic feed需要），两会话隔离。
        """
        if self._sub_session is None or self._sub_session.closed:
            async with self._session_lock:
                if self._sub_session is None or self._sub_session.closed:
                    connector = aiohttp.TCPConnector(limit=5)
                    timeout = aiohttp.ClientTimeout(total=30)
                    self._sub_session = aiohttp.ClientSession(
                        headers=HEADERS, connector=connector, timeout=timeout,
                        cookie_jar=aiohttp.CookieJar(),
                        # 评论/子评论通道：默认代理（trust_env读HTTP_PROXY），
                        # comment.direct=True 时直连（不读环境变量），防机场故障连累主功能
                        trust_env=not self._comment_direct,
                    )
        return self._sub_session

    async def close(self):
        """关闭HTTP会话，释放连接资源"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        if self._sub_session and not self._sub_session.closed:
            await self._sub_session.close()
            self._sub_session = None
        logger.info("HTTP会话已关闭")

    async def _ensure_buvid(self):
        """访问B站首页获取buvid3等匿名Cookie，成功后不再重复；失败按 BUVID_RETRY_INTERVAL 节流重试，不阻塞"""
        if self._buvid_ready:
            return
        if time.time() - self._buvid_failed_at < BUVID_RETRY_INTERVAL:
            return
        try:
            session = await self._get_session()
            async with session.get(
                "https://www.bilibili.com/",
                headers={**HEADERS, "Referer": "https://www.bilibili.com/"},
            ) as resp:
                await resp.read()
            self._buvid_ready = True
            logger.debug("buvid3匿名Cookie已获取")
        except Exception as e:
            logger.warning(f"获取buvid失败: {e}")
            self._buvid_failed_at = time.time()

    async def _fetch_mixin_key(self) -> str:
        """
        获取WBI签名密钥，结果缓存MIXIN_KEY_TTL秒。
        nav接口即使未登录也会返回wbi_img数据，code=-101时仍可用。
        """
        now = time.time()
        if self._mixin_key and (now - self._mixin_key_ts) < MIXIN_KEY_TTL:
            return self._mixin_key

        session = await self._get_session()
        async with session.get(NAV_URL) as resp:
            data = await resp.json()
        # nav接口：code=-101（未登录）仍包含wbi_img，不应视为错误
        wbi_img = (data.get("data") or {}).get("wbi_img")
        if not wbi_img:
            raise RuntimeError(
                f"获取WBI密钥失败: code={data.get('code')} msg={data.get('message')}"
            )
        self._mixin_key = get_mixin_key_from_urls(
            wbi_img["img_url"], wbi_img["sub_url"]
        )
        self._mixin_key_ts = now
        logger.debug("WBI密钥已刷新")
        return self._mixin_key

    async def _signed_get(self, url: str, params: dict) -> dict:
        """带WBI签名的GET请求，返回data字段，非0code抛RuntimeError"""
        mixin = await self._fetch_mixin_key()
        signed = wbi_sign_params(params, mixin)
        session = await self._get_session()
        async with session.get(url, params=signed) as resp:
            data = await resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"API错误 url={url} code={data.get('code')} msg={data.get('message')}")
        return data.get("data", {})

    async def _unsigned_get(self, url: str, params: dict = None) -> dict:
        """无需签名的GET请求，返回data字段，非0code抛RuntimeError"""
        session = await self._get_session()
        async with session.get(url, params=params or {}) as resp:
            data = await resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"API错误 url={url} code={data.get('code')} msg={data.get('message')}")
        return data.get("data", {})

    # ----------------------------------------------------------
    # 置顶动态自动检测（Priority 自动模式）
    # ----------------------------------------------------------

    async def get_pinned_dynamic_id(self, uid: str) -> Optional[str]:
        """
        从空间动态列表中自动发现置顶动态ID。

        通过检查每条动态的 modules.module_tag.text 是否为"置顶"来判断。
        使用 polymer space feed API（零Cookie + WBI签名），与 BTCE 方案一致。

        风控处理：-412 等瞬时风控在本函数内轻量重试（4s x 3 次），
        重试耗尽后抛 RuntimeError（由调用方决定跳过本轮），
        返回 None 仅表示 API 正常但确实无置顶动态 —— 两者严格区分，
        防止调用方把 API 失败误判为"置顶已消失"。

        Args:
            uid: B站 UID

        Returns:
            置顶动态的 id_str，API 正常但无置顶动态时返回 None

        Raises:
            RuntimeError: API 重试后仍失败（风控/网络问题）
        """
        await self._ensure_buvid()
        # 轻量重试：B站对 space feed 有间歇性 -412 风控（约20%概率，几秒恢复）
        data = None
        for attempt in range(3):
            try:
                data = await self._signed_get(SPACE_FEED_URL, {"host_mid": uid})
                break
            except RuntimeError as e:
                if attempt < 2:
                    logger.warning(f"获取置顶动态失败 uid={uid}: {e}，4s后重试")
                    await asyncio.sleep(4)
                else:
                    logger.warning(f"获取置顶动态失败 uid={uid}: {e}")
                    raise

        raw_items = data.get("items", [])
        for item in raw_items:
            modules = item.get("modules", {})
            tag = modules.get("module_tag") or {}
            if tag.get("text") == "置顶":
                pinned_id = item.get("id_str", "")
                if pinned_id:
                    # 提取模块信息用于日志
                    mod_dyn = modules.get("module_dynamic") or {}
                    desc = mod_dyn.get("desc") or {}
                    text_preview = (desc.get("text", "") or "")[:50]
                    logger.info(f"发现置顶动态: {pinned_id} ({text_preview}...)")
                    return pinned_id

        logger.debug(f"未发现置顶动态 uid={uid}")
        return None

    # ----------------------------------------------------------
    # 用户信息
    # ----------------------------------------------------------

    @async_retry(API_RETRY)
    async def get_user_info(self, uid: str) -> dict:
        """
        获取用户公开信息（无需登录，使用card公开接口）。

        Returns:
            {"name": 昵称, "uid": UID, "face": 头像URL}
        """
        data = await self._unsigned_get(USER_INFO_URL, {"mid": uid})
        card = data.get("card", {})
        info = {
            "name": card.get("name", ""),
            "uid": str(card.get("mid", "")),
            "face": card.get("face", ""),
        }
        logger.info(f"获取用户信息: {info['name']} (UID: {uid})")
        return info

    # ----------------------------------------------------------
    # 用户动态列表（场景一发现）
    # ----------------------------------------------------------

    @async_retry(API_RETRY)
    async def get_user_dynamics(self, uid: str) -> list[dict]:
        """
        获取UP主空间动态列表。

        策略：
        1. 获取buvid3（匿名设备Cookie，仅需一次）
        2. 调polymer空间API（带buvid即可，无需登录态）
        3. 失败则降级为WBI视频搜索
        4. 风控（-352/-412）不重试：快速重试是bot特征会延长风控，
           优先返回最近成功结果的缓存兜底（见 DYN_CACHE_MAX_AGE）

        Returns:
            [{dynamic_id, comment_oid, type, content, pub_ts}, ...]
        """
        # 确保有buvid3（首次访问B站首页获取，仅需一次）
        await self._ensure_buvid()

        # 风控熔断：冷却期内不发起任何请求，直接缓存兜底（无缓存返回空列表），
        # 防止 -412 期间"边被封边疯狂降级"的反向恶化与资源空耗
        if self._is_breaker_open():
            cached = self._get_dyn_cache(uid)
            if cached is not None:
                return cached
            return []

        # 空间动态API需要 buvid3 + WBI签名（缺一不可）
        try:
            data = await self._signed_get(SPACE_FEED_URL, {"host_mid": uid})
            items = data.get("items", [])
            if items:
                result = _parse_space_feed(data)
                self._update_dyn_cache(uid, result)
                logger.info(f"获取 {uid} 空间动态: {len(items)} 条")
                return result
        except RuntimeError as e:
            if self._is_risk_control_error(e):
                # 风控命中计入熔断计数（空间动态+降级搜索各计1次）
                self._record_ratelimit_hit()
            logger.info(f"空间动态API失败，降级视频搜索 uid={uid}: {e}")

        # 降级：WBI签名视频搜索（仅覆盖视频作品）
        try:
            result = await self._get_user_videos_wbi(uid)
            self._update_dyn_cache(uid, result)
            return result
        except RuntimeError as e:
            # 风控错误：放弃本轮（不重试），有未过期缓存则兜底返回
            if self._is_risk_control_error(e):
                self._record_ratelimit_hit()
                cached = self._get_dyn_cache(uid)
                if cached is not None:
                    return cached
                logger.warning(f"动态列表风控且无可用缓存，放弃本轮: {e}")
                return []
            # 其他瞬态错误（网络超时等）交给 async_retry 重试
            raise

    async def _get_user_videos_wbi(self, uid: str) -> list[dict]:
        """WBI签名视频搜索（空间动态不可用时的降级方案）"""
        url = "https://api.bilibili.com/x/space/wbi/arc/search"
        data = await self._signed_get(url, {"mid": uid, "ps": 30, "pn": 1})
        vlist = (data.get("list") or {}).get("vlist") or []
        result = [{
            "dynamic_id": f"av{v.get('aid', 0)}",
            "item_id": f"av{v.get('aid', 0)}",
            "comment_oid": str(v.get("aid", "")),
            "type": "DYNAMIC_TYPE_AV",
            "content": v.get("title", ""),
            "pub_ts": v.get("created", 0),
        } for v in vlist]
        logger.info(f"获取 {uid} 视频列表(降级): {len(result)} 条")
        return result

    # ----------------------------------------------------------
    # 动态列表缓存（风控兜底）
    # ----------------------------------------------------------

    @staticmethod
    def _is_risk_control_error(e: RuntimeError) -> bool:
        """判断是否为风控类错误（-352风控校验失败 / -412请求被封禁），这类错误重试无益"""
        msg = str(e)
        return "code=-352" in msg or "code=-412" in msg

    def _is_breaker_open(self) -> bool:
        """
        风控熔断是否生效：冷却期内不再请求B站接口（缓存兜底），到期自动解除。

        Returns:
            True=熔断中（调用方应跳过正式请求，返回缓存/空）
        """
        if time.time() < self._rl_cooldown_until:
            return True
        if self._rl_cooldown_until > 0:
            # 冷却到期：复位熔断标志，恢复正式轮询
            self._rl_cooldown_until = 0.0
            logger.info("风控熔断解除，恢复空间动态轮询")
        return False

    def _record_ratelimit_hit(self) -> None:
        """记录一次风控命中（-352/-412）；连续命中达到阈值触发熔断冷却"""
        self._rl_hit_count += 1
        if self._rl_hit_count >= RATELIMIT_BREAKER_HITS:
            self._rl_hit_count = 0
            self._rl_cooldown_until = time.time() + self._breaker_cooldown_seconds
            logger.warning(
                f"风控连续命中 {RATELIMIT_BREAKER_HITS} 次，触发熔断 "
                f"{self._breaker_cooldown_seconds}s（期间跳过空间动态轮询）"
            )

    def _update_dyn_cache(self, uid: str, result: list) -> None:
        """缓存最近一次成功的动态列表（风控期间兜底用）"""
        self._dyn_cache[uid] = (time.time(), result)

    def _get_dyn_cache(self, uid: str) -> Optional[list]:
        """取未过期的动态列表缓存，无缓存或已过期返回 None"""
        cached = self._dyn_cache.get(uid)
        if cached is None:
            return None
        ts, result = cached
        if time.time() - ts > DYN_CACHE_MAX_AGE:
            self._dyn_cache.pop(uid, None)
            return None
        logger.warning(f"风控兜底：使用 {uid} 的动态列表缓存 ({len(result)} 条)")
        return result

    def match_dynamic_id_from_cache(self, uid: str, av_item_id: str) -> Optional[str]:
        """
        从内存动态列表缓存中查找 av 降级项对应的真实动态ID（富化辅助，零API成本）。

        背景：av{aid} 降级项没有动态页可截（空间动态API风控期间的视频搜索降级），
        而动态列表缓存来自正常 feed（含真实动态ID），按键 aid 匹配即可还原
        （2026-09-04 截图 bug 修复）。

        Args:
            uid: UP主 uid（缓存按 uid 分）
            av_item_id: av 前缀 item_id（如 av123456789012）

        Returns:
            真实动态ID（纯数字）；缓存未命中或没有对应视频返回 None
        """
        if not av_item_id.startswith("av") or not uid:
            return None
        aid = av_item_id[2:]
        cached = self._get_dyn_cache(uid)
        if not cached:
            return None
        for item in cached:
            if str(item.get("comment_oid", "")) == aid and not str(item.get("dynamic_id", "")).startswith("av"):
                return item.get("dynamic_id", "")
        return None

    # ----------------------------------------------------------
    # 评论获取（场景一+二共用）
    # ----------------------------------------------------------

    @async_retry(API_RETRY)
    async def get_comments(
        self,
        oid: int,
        comment_type: int,
        pagination_str: str = "",
        mode: int = 2,
    ) -> dict:
        """
        获取评论区一级评论（游标翻页，需WBI签名）。

        确定性错误处理：-400（游标无效/参数错误）、-404（无评论）、
        12002（评论功能已关闭）重试无意义，直接返回空结果（is_end=True），
        避免 async_retry 的 50s×3 重试阻塞轮询循环（priority 每 1~5s 一轮，
        无锁并发会因重试堆积触发请求风暴）。

        Args:
            oid: 评论区线程ID（数字，来自dynamic_detail的comment_id_str）
            comment_type: 评论区类型整数（1=视频 11=图文 12=专栏 17=文字）
            pagination_str: 翻页游标裸值。首次不传（空字符串）；后续传 cursor.pagination_reply.next_offset
            mode: 排序方式（2=按时间 3=按热度，默认2）

        Returns:
            {"replies": [评论对象], "cursor": {...}, "top_replies": [置顶评论]}

        注意：B站 要求 pagination_str 传 {"offset": <裸值>} 的 JSON 格式，
        直接传裸字符串会 -400（2026-08-11 实测复现，参考 Freeview 插件实现）。
        函数内部负责 JSON 包装，调用方只需传裸值。

        干净会话说明（2026-08-19 实测）：主会话种过 buvid 后（space feed/topic feed
        调用过），本接口可见评论条数会被 B站 降级（9条 → 3条，关键楼消失）。
        与子评论翻页同一机理，必须走独立干净会话 _get_sub_session。
        """
        params = {"oid": oid, "type": comment_type, "mode": mode}
        if pagination_str and pagination_str != "0":
            params["pagination_str"] = json.dumps({"offset": pagination_str})

        try:
            # 走干净会话 + 手动WBI签名（主会话的 buvid 会触发评论降级）
            mixin = await self._fetch_mixin_key()
            signed = wbi_sign_params(params, mixin)
            session = await self._get_sub_session()
            async with session.get(MAIN_COMMENT_URL, params=signed) as resp:
                raw_data = await resp.json()
            if raw_data.get("code") != 0:
                raise RuntimeError(
                    f"API错误 url={MAIN_COMMENT_URL} code={raw_data.get('code')} msg={raw_data.get('message')}"
                )
            data = raw_data.get("data", {})
        except RuntimeError as e:
            # 确定性错误码：重试无意义，快速失败返回空
            if any(f"code={code}" in str(e) for code in (-400, -404, 12002)):
                logger.warning(f"get_comments 确定性失败(不重试) oid={oid}: {e}")
                return {"replies": [], "cursor": {"is_end": True}, "top_replies": [], "disabled": True}
            raise
        return {
            "replies": data.get("replies") or [],
            "cursor": data.get("cursor", {}),
            "top_replies": data.get("top_replies") or [],
        }

    @async_retry(API_RETRY)
    async def get_sub_comments(
        self,
        oid: int,
        comment_type: int,
        root_rpid: int,
        page_index: int = 1,
    ) -> dict:
        """
        获取子评论（楼中楼），无需WBI签名。

        必须走独立干净会话（_get_sub_session）：主会话种过buvid后，
        本接口第2页起静默返回空，翻页失效（详见_get_sub_session注释）。

        确定性错误处理：12002（评论功能已关闭）、12022（评论已被删除）、-400/-404 重试无意义，
        直接返回空结果（disabled=True），避免 50s×3 重试阻塞 sweep 循环。

        Args:
            oid: 评论区线程ID
            comment_type: 评论区类型整数
            root_rpid: 根评论的rpid（要展开的楼层）
            page_index: 页码（从1开始）

        Returns:
            {"replies": [子评论对象], "page": {"count": 总数, "num": 当前页, "size": 每页数}}
        """
        params = {
            "oid": oid,
            "type": comment_type,
            "root": root_rpid,
            "pn": page_index,
            "ps": SUB_COMMENT_PAGE_SIZE,
        }
        session = await self._get_sub_session()
        try:
            async with session.get(SUB_COMMENT_URL, params=params) as resp:
                raw = await resp.json()
            if raw.get("code") != 0:
                raise RuntimeError(
                    f"API错误 url={SUB_COMMENT_URL} code={raw.get('code')} msg={raw.get('message')}"
                )
        except RuntimeError as e:
            # 确定性错误码：重试无意义，快速失败返回空
            if any(f"code={code}" in str(e) for code in (-400, -404, 12002, 12022)):
                logger.warning(f"get_sub_comments 确定性失败(不重试) root={root_rpid}: {e}")
                return {"replies": [], "page": {"count": 0}, "disabled": True}
            # -412 风控：重试窗口期内大概率持续失败，50s×3 纯刷错误日志。
            # 快速失败返回 banned 标志，调用方跳过本轮，下轮扫查再试
            if "code=-412" in str(e):
                logger.warning(f"get_sub_comments 风控跳过(不重试) root={root_rpid}: {e}")
                return {"replies": [], "page": {"count": 0}, "banned": True}
            raise
        # 注意：响应结构是 {"code":0, "data":{replies/page}}，取内层 data
        data = raw.get("data") or {}
        return {
            "replies": data.get("replies") or [],
            "page": data.get("page", {}),
        }

    # ----------------------------------------------------------
    # 话题动态列表（场景二发现）
    # ----------------------------------------------------------

    @async_retry(API_RETRY)
    async def get_topic_cards(self, topic_id: int, offset: str = "") -> dict:
        """
        获取话题下最新动态列表（公开接口，需buvid3 + WBI签名）。

        排序值由 topic_sort_by_conf 动态决定（非固定0/1），需从API响应中读取。
        响应数据在 data.topic_card_list.items 中（非 data.items），
        与空间动态 API 的响应结构不同。

        Args:
            topic_id: 话题ID
            offset: 分页游标（首次传空字符串，后续传 topic_card_list.offset）

        Returns:
            {"items": [{dynamic_id, comment_oid, type, content, rich_content, pub_ts}],
             "has_more": bool, "offset": str}
        """
        # 确保 buvid3 匿名设备指纹已获取（polymer API 必要条件）
        await self._ensure_buvid()

        # 首次调用：不带 sort_by 获取 topic_sort_by_conf，提取"最新"对应的值
        sort_by = self._topic_sort_by_cache.get(topic_id)
        if sort_by is None:
            sort_by = await self._resolve_topic_sort_by(topic_id)

        params = {
            "topic_id": topic_id,
            "sort_by": sort_by,
            "offset": offset,
            "page_size": 20,
            "source": "Web",
            "features": "itemOpusStyle,listOnlyfans,opusBigCover",
        }
        data = await self._signed_get(TOPIC_FEED_URL, params)

        # 话题动态的响应结构：data.topic_card_list.items（非 data.items）
        topic_card_list = data.get("topic_card_list", {})
        raw_items = topic_card_list.get("items", [])

        items = []
        for item in raw_items:
            # 话题动态的item结构: {dynamic_card_item: {...}, topic_type: "DYNAMIC"}
            # 实际动态数据都嵌套在 dynamic_card_item 里，需解包一层
            dci = item.get("dynamic_card_item", {})
            if not dci:
                continue

            dyn_id = dci.get("id_str", "")
            dyn_type = dci.get("type", "")
            if not dyn_id or not dyn_type:
                continue

            basic = dci.get("basic", {})
            comment_oid = str(basic.get("comment_id_str", ""))

            # 视频类型：评论区oid用aid（与 _parse_space_feed 一致）
            modules = dci.get("modules", {})
            mod_dyn = modules.get("module_dynamic") or {}
            major = mod_dyn.get("major") or {}
            archive = major.get("archive") or {}
            aid = str(archive.get("aid", ""))
            if dyn_type == "DYNAMIC_TYPE_AV" and aid:
                comment_oid = aid

            # 降级：用 rid_str 或 dyn_id 兜底
            if not comment_oid:
                comment_oid = str(basic.get("rid_str", ""))
            if not comment_oid:
                comment_oid = dyn_id

            # 提取帖子正文（纯文本，用于邮件上下文展示）
            # 直接从 rich_text_nodes 提取，这是 B站前端渲染用的权威数据源
            desc = mod_dyn.get("desc") if isinstance(mod_dyn.get("desc"), dict) else {}
            post_content = _extract_text_from_rich_nodes(
                desc.get("rich_text_nodes")
            )

            # 提取帖子富内容（表情+图片+视频，用于邮件渲染）
            post_rich_content = _extract_post_rich_content(dci)

            # 提取帖子发布时间（用于正确归档，避免旧帖被当新帖）
            mod_author = modules.get("module_author") or {}
            pub_ts = mod_author.get("pub_ts", 0)

            items.append({
                "dynamic_id": dyn_id,
                "item_id": dyn_id,
                "comment_oid": comment_oid,
                "type": dyn_type,
                "content": post_content,
                "rich_content": post_rich_content,
                "pub_ts": pub_ts,
            })

        logger.info(f"获取话题 {topic_id} 动态: {len(items)} 条 (sort_by={sort_by})")
        return {
            "items": items,
            "has_more": topic_card_list.get("has_more", False),
            "offset": topic_card_list.get("offset", ""),
        }

    async def _resolve_topic_sort_by(self, topic_id: int) -> int:
        """
        从 topic_sort_by_conf 中解析"最新"对应的 sort_by 值。

        不同话题的排序值不同（如话题A用3，话题B用1），不能硬编码。
        结果缓存到 _topic_sort_by_cache，避免每次查询。
        """
        try:
            params = {
                "topic_id": topic_id,
                "offset": "",
                "page_size": 1,
                "source": "Web",
                "features": "itemOpusStyle,listOnlyfans,opusBigCover",
            }
            data = await self._signed_get(TOPIC_FEED_URL, params)
            tcl = data.get("topic_card_list", {})
            sort_conf = tcl.get("topic_sort_by_conf", {})
            for entry in sort_conf.get("all_sort_by", []):
                if entry.get("sort_name") == "最新":
                    sort_by = entry.get("sort_by", 3)
                    self._topic_sort_by_cache[topic_id] = sort_by
                    logger.info(f"话题 {topic_id} 最新排序 sort_by={sort_by}")
                    return sort_by
        except Exception as e:
            logger.warning(f"解析话题排序配置失败 topic={topic_id}: {e}")

        # 兜底：多数话题用 sort_by=3 表示最新
        fallback = 3
        self._topic_sort_by_cache[topic_id] = fallback
        return fallback

    # ----------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------

    @staticmethod
    def get_comment_type(dynamic_type: str) -> int:
        """根据动态类型字符串返回评论区类型整数"""
        return DYNAMIC_TYPE_TO_COMMENT.get(dynamic_type, DEFAULT_COMMENT_TYPE)

    @staticmethod
    def get_oid(item_id: str, item_type: str = "") -> int:
        """
        将作品ID转为整数oid。

        - 纯数字ID（dynamic_id/aid）: 直接转int
        - 非数字（如BV号）: 返回0，调用方自行降级处理
        """
        try:
            return int(item_id)
        except (ValueError, TypeError):
            logger.warning(f"无法将 item_id={item_id} 转为整数oid")
            return 0
