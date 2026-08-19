# notifier.py
"""EchoWatch 邮件通知模块 —— 即时通知 + 日报摘要"""

import asyncio
import base64
import html
import json
import random
import re
import smtplib
import time
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from config import Config
from database import Database
from logger_config import logger


# ============================================================
# 随机主题系统 —— 氛围色板 + 纹理 + 光影 + 分割线
# ============================================================

# 弃用氛围色板，色相完全随机 0-360，每次都不一样
_MOODS = [{"name": "随机", "hue": (0, 360), "sat": (50, 72), "light": (42, 55)}]

# 四种顶栏纹理：纯CSS背景图案，叠加在渐变上
_TEXTURES = [
    # 星河：圆点星 + 4颗十字星芒（均匀分布左右平衡），光芒加粗加亮
    "radial-gradient(circle at 12% 18%, rgba(255,255,255,0.30) 3px, transparent 3px), "
    "radial-gradient(ellipse 16px 3px at 12% 18%, rgba(255,255,255,0.20) 0%, transparent 100%), "
    "radial-gradient(ellipse 3px 16px at 12% 18%, rgba(255,255,255,0.20) 0%, transparent 100%), "
    "radial-gradient(circle at 48% 58%, rgba(255,255,255,0.24) 3.5px, transparent 3.5px), "
    "radial-gradient(ellipse 18px 3.5px at 48% 58%, rgba(255,255,255,0.17) 0%, transparent 100%), "
    "radial-gradient(ellipse 3.5px 18px at 48% 58%, rgba(255,255,255,0.17) 0%, transparent 100%), "
    "radial-gradient(circle at 78% 16%, rgba(255,255,255,0.28) 3px, transparent 3px), "
    "radial-gradient(ellipse 16px 3px at 78% 16%, rgba(255,255,255,0.19) 0%, transparent 100%), "
    "radial-gradient(ellipse 3px 16px at 78% 16%, rgba(255,255,255,0.19) 0%, transparent 100%), "
    "radial-gradient(circle at 25% 72%, rgba(255,255,255,0.22) 3.5px, transparent 3.5px), "
    "radial-gradient(ellipse 17px 3.5px at 25% 72%, rgba(255,255,255,0.18) 0%, transparent 100%), "
    "radial-gradient(ellipse 3.5px 17px at 25% 72%, rgba(255,255,255,0.18) 0%, transparent 100%), "
    "radial-gradient(circle at 88% 62%, rgba(255,255,255,0.20) 2.5px, transparent 2.5px), "
    "radial-gradient(circle at 58% 35%, rgba(255,255,255,0.18) 2.5px, transparent 2.5px), "
    "radial-gradient(circle at 20% 42%, rgba(255,255,255,0.16) 2px, transparent 2px), "
    "radial-gradient(circle at 68% 80%, rgba(255,255,255,0.17) 2px, transparent 2px)",
    # 月辉：双弦月 + 伴星（保持不变）
    "radial-gradient(circle at 78% 16%, rgba(255,255,255,0.26) 0%, transparent 24px), "
    "radial-gradient(circle at 73% 22%, rgba(0,0,0,0.12) 0%, transparent 18px), "
    "radial-gradient(circle at 82% 10%, rgba(255,255,255,0.32) 0%, transparent 8px), "
    "radial-gradient(circle at 30% 62%, rgba(255,255,255,0.22) 0%, transparent 16px), "
    "radial-gradient(circle at 26% 56%, rgba(0,0,0,0.10) 0%, transparent 12px), "
    "radial-gradient(circle at 34% 58%, rgba(255,255,255,0.26) 0%, transparent 5px), "
    "radial-gradient(circle at 8% 40%, rgba(255,255,255,0.16) 2.5px, transparent 2.5px), "
    "radial-gradient(circle at 52% 28%, rgba(255,255,255,0.15) 2px, transparent 2px), "
    "radial-gradient(circle at 55% 70%, rgba(255,255,255,0.14) 2px, transparent 2px), "
    "radial-gradient(circle at 18% 75%, rgba(255,255,255,0.15) 1.5px, transparent 1.5px), "
    "radial-gradient(circle at 88% 55%, rgba(255,255,255,0.14) 2px, transparent 2px), "
    "radial-gradient(circle at 92% 50%, rgba(255,255,255,0.13) 1.5px, transparent 1.5px)",
    # 流光：3颗流星统一↗，左中右均衡分布，高度错落
    "radial-gradient(circle at 18% 72%, rgba(255,255,255,0.32) 2.5px, transparent 3px), "
    "linear-gradient(42deg, transparent 52%, rgba(255,215,0,0.02) 68%, rgba(255,215,0,0.10) 71%, rgba(255,215,0,0.02) 72%, transparent 73%), "
    "radial-gradient(circle at 50% 55%, rgba(255,255,255,0.30) 2px, transparent 2.5px), "
    "linear-gradient(42deg, transparent 35%, rgba(255,255,255,0.02) 50%, rgba(255,255,255,0.12) 54%, rgba(255,255,255,0.02) 55%, transparent 56%), "
    "radial-gradient(circle at 82% 68%, rgba(255,255,255,0.28) 2px, transparent 2.5px), "
    "linear-gradient(42deg, transparent 48%, rgba(200,200,220,0.02) 63%, rgba(200,200,220,0.08) 67%, rgba(200,200,220,0.01) 68%, transparent 69%)",
    # 涟漪：细环浮雕，明暗交替，环宽缩窄
    "radial-gradient(circle at 48% 36%, rgba(255,255,255,0.22) 0%, transparent 18%), "
    "radial-gradient(circle at 48% 36%, transparent 32%, rgba(0,0,0,0.07) 34%, rgba(0,0,0,0.07) 35.5%, transparent 37%), "
    "radial-gradient(circle at 48% 36%, transparent 52%, rgba(255,255,255,0.14) 54%, rgba(255,255,255,0.14) 55.5%, transparent 57%), "
    "radial-gradient(circle at 48% 36%, transparent 70%, rgba(0,0,0,0.05) 72%, rgba(0,0,0,0.05) 73.5%, transparent 75%)",
]

# 纹理名列表（与 _TEXTURES 一一对应，用于预览文件名）
_TEXTURE_NAMES = ["星河", "月辉", "流光", "涟漪"]

# 三种卡片阴影深度
_SHADOWS = [
    "0 1px 6px rgba(0,0,0,0.06)",     # 轻盈
    "0 2px 12px rgba(0,0,0,0.10)",    # 标准
    "0 4px 20px rgba(0,0,0,0.12)",    # 厚重
]

# 四种分割线样式
_DIVIDER_TEMPLATES = [
    "border: 0; height: 2px; background: linear-gradient(90deg, {p}, {s});",
    "border: 0; border-top: 2px dotted #ccc;",
    "border: 0; border-top: 1px solid #e0e0e0; border-bottom: 1px solid #f0f0f0; height: 3px;",
    "border: 0; border-top: 1px dashed #d0d0d0;",
]


def _hsl_to_hex(h: int, s: int, l: int) -> str:
    """HSL 转 HEX 颜色"""
    s /= 100.0
    l /= 100.0
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    r, g, b = 0, 0, 0
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    r = int((r + m) * 255)
    g = int((g + m) * 255)
    b = int((b + m) * 255)
    return f"#{r:02x}{g:02x}{b:02x}"


def random_theme() -> dict:
    """
    生成一套完整的随机主题。

    包含：主色、副色、氛围名、顶栏纹理、卡片阴影、分割线样式。

    策略：
    1. 从 6 种氛围中随机选一种，在氛围色相范围内取色 —— 保证不翻车
    2. 副色 50% 互补色 / 50% 邻近色 —— 保留随机感
    3. 纹理、阴影、分割线各从预设池随机抽取
    """
    mood = random.choice(_MOODS)
    hue = random.randint(*mood["hue"])
    sat = random.randint(*mood["sat"])
    light = random.randint(*mood["light"])
    primary = _hsl_to_hex(hue, sat, light)

    # 副色：50%互补色 / 50%邻近色
    if random.random() < 0.5:
        sec_hue = (hue + 180) % 360
    else:
        sec_hue = (hue + random.choice([-30, 30])) % 360
    sec_sat = min(100, sat + random.randint(-10, 15))
    sec_light = min(72, max(48, light + random.randint(5, 20)))
    secondary = _hsl_to_hex(sec_hue, sec_sat, sec_light)

    tex_idx = random.randrange(len(_TEXTURES))
    return {
        "primary": primary,
        "secondary": secondary,
        "mood_name": mood["name"],
        "texture": _TEXTURES[tex_idx],
        "texture_name": _TEXTURE_NAMES[tex_idx],
        "shadow": random.choice(_SHADOWS),
        "divider": random.choice(_DIVIDER_TEMPLATES).format(p=primary, s=secondary),
        "radius": 10,
    }


# ============================================================
# 评论链接生成
# ============================================================

def _resolve_aid(item_id: str, aid: str) -> str:
    """
    解析真实 aid：优先取 comment_oid 传入值；
    缺省时剥 item_id 的 av 前缀（空间动态 API 风控降级路径的产物，如 av117032272005640）。
    """
    if aid:
        return aid
    if item_id.startswith("av") and item_id[2:].isdigit():
        return item_id[2:]
    return ""


def build_comment_url(item_id: str, item_type: str, rpid, aid: str = "") -> str:
    """
    生成评论直达链接。

    - 视频（item_type 含 AV 或 item_id 带 av 前缀）: https://www.bilibili.com/video/av{aid}/#reply{rpid}
    - 动态: https://t.bilibili.com/{dynamic_id}#reply{rpid}

    aid 优先取 comment_oid（真实 aid，item_id 是动态ID时必需）；
    视频类但 aid 缺失时降级为动态链接。
    """
    # 视频类判定：item_type 含 AV，或 item_id 自带 av 前缀（批量邮件不传 item_type 时靠此识别）
    is_video = (item_type and "AV" in item_type) or item_id.startswith("av")
    if is_video:
        real_aid = _resolve_aid(item_id, aid)
        if real_aid:
            return f"https://www.bilibili.com/video/av{real_aid}/#reply{rpid}"
    return f"https://t.bilibili.com/{item_id}#reply{rpid}"


def build_item_url(item_id: str, item_type: str, aid: str = "") -> str:
    """生成作品链接（不带评论锚点），规则同 build_comment_url"""
    is_video = (item_type and "AV" in item_type) or item_id.startswith("av")
    if is_video:
        real_aid = _resolve_aid(item_id, aid)
        if real_aid:
            return f"https://www.bilibili.com/video/av{real_aid}"
    return f"https://t.bilibili.com/{item_id}"


# ============================================================
# 评论内容渲染（文本+表情+图片 → HTML 片段）
# ============================================================

def render_comment_html(content_text: str, rich_content_str: str = None) -> str:
    """
    将评论文本和富内容渲染为邮件 HTML 片段。

    处理流程：
    1. rich_content 为空时降级为纯文本（html.escape 转义）
    2. 用 emote 映射把 [表情名] 占位符替换为 img 标签
    3. 追加 pictures 图片列表（flex 容器）
    4. 处理 jump_url 超链接

    Args:
        content_text: 纯文本内容（含 [表情名] 占位符）
        rich_content_str: 富内容 JSON 字符串（含 emote/pictures/jump_url）

    Returns:
        HTML 片段字符串
    """
    # 降级：无富内容时返回转义后的纯文本
    if not rich_content_str:
        return _escape_text(content_text)

    try:
        rich = json.loads(rich_content_str)
    except (json.JSONDecodeError, TypeError):
        return _escape_text(content_text)

    # 1. HTML 转义基础文本
    msg = html.escape(content_text or "")

    # 2. 替换 B站表情占位符为 img 标签
    emote_map = rich.get("emote", {})
    if emote_map:
        for key, info in emote_map.items():
            url = _safe_url(info.get("url", ""))
            if not url:
                continue
            # 根据 meta.size 选样式类：1=小表情 2=大表情
            meta = info.get("meta", {})
            size = meta.get("size", 1) if isinstance(meta, dict) else 1
            size_cls = "emote-large" if size == 2 else "emote-small"
            escaped_key = html.escape(key)
            img_tag = (
                f'<img class="{size_cls}" src="{html.escape(url)}"'
                f' alt="{escaped_key}" title="{escaped_key}">'
            )
            msg = msg.replace(escaped_key, img_tag)

    # 3. 保留换行
    msg = msg.replace("\n", "<br>")

    # 4. 评论区图片（追加到文本末尾）
    pictures_html = ""
    pics = rich.get("pictures", [])
    if pics:
        imgs_parts = []
        for p in pics:
            src = p.get("img_src", "")
            if not src:
                continue
            # 去掉 @后缀水印参数，获取原图
            if "@" in src:
                src = src.split("@")[0]
            src = _safe_url(src)
            if not src:
                continue
            imgs_parts.append(
                f'<img class="comment-image" src="{html.escape(src)}" alt="评论图片">'
            )
        if imgs_parts:
            pictures_html = (
                '<div class="images-container">'
                + "".join(imgs_parts)
                + "</div>"
            )

    # 5. 超链接（jump_url）：不渲染（2026-08-19 XTong 要求清洗）
    # B站 会为评论中的关键词自动生成"评论内容自动搜索"链接（如"你真好"→ 搜索页），
    # 对邮件读者是噪音且像广告，全部跳过。用户手动发的链接在 message 中仍以纯文本显示。

    return msg + pictures_html


def _escape_text(text: str) -> str:
    """纯文本降级：HTML转义并保留换行"""
    if not text:
        return ""
    return html.escape(text).replace("\n", "<br>")


def _safe_url(url: str, default: str = "") -> str:
    """
    仅允许 http/https 协议的 URL，其它一律返回 default。
    防 javascript: / data: / vbscript: 等协议注入（用于 img src 与 a href）。
    """
    if not url:
        return default
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if url.lower().startswith(("http://", "https://")):
        return url
    return default


def _safe_filename(name: str, max_len: int = 100) -> str:
    """清洗文件名：仅保留安全字符，防路径穿越/特殊字符注入"""
    if not name:
        return "unnamed"
    cleaned = re.sub(r'[^\w.\-\u4e00-\u9fff]', "_", name)
    cleaned = cleaned.strip("._")
    return cleaned[:max_len] or "unnamed"


# ============================================================
# 主播金色签名（base64 内嵌，右下角展示）
# ============================================================

# ============================================================
# 邮件 HTML 构建
# ============================================================

def _base_style(t: dict) -> str:
    """
    生成邮件基础 CSS。

    Args:
        t: random_theme() 返回的主题 dict（primary, secondary, texture, shadow, divider, radius）
    """
    p = t["primary"]
    s = t["secondary"]
    return f"""
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 0; padding: 10px; background-color: #f5f5f5; }}
        .container {{ max-width: 600px; width: 100%; margin: 0 auto; background-color: #fff; border-radius: {t["radius"]}px; box-shadow: {t["shadow"]}; overflow: hidden; }}
        .header {{ background: {p}; background: {t["texture"]}, linear-gradient(135deg, {p}, {s}); color: #fff; padding: 25px 20px; text-align: center; }}
        .header h1 {{ margin: 0 0 5px 0; font-size: 22px; }}
        .header p {{ margin: 0; font-size: 13px; opacity: 0.85; }}
        .content {{ padding: 15px; }}
        .interaction {{ background: #fafafa; border-left: 4px solid {p}; padding: 12px; margin-bottom: 12px; border-radius: 0 6px 6px 0; }}
        .interaction .label {{ font-size: 12px; color: #999; }}
        .interaction .text {{ margin: 6px 0; white-space: pre-wrap; word-break: break-word; line-height: 1.6; }}
        .context-box {{ background: #f0f0f0; border-left: 3px solid #ccc; padding: 8px 12px; margin: 8px 0; border-radius: 4px; font-size: 13px; color: #666; }}
        .btn {{ display: inline-block; background: {p}; background: linear-gradient(135deg, {p}, {s}); color: #fff; padding: 8px 18px; border-radius: 4px; text-decoration: none; font-size: 13px; font-weight: bold; margin-right: 8px; }}
        .badge {{ display: inline-block; background: {p}; background: linear-gradient(135deg, {p}, {s}); color: #fff; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin-right: 5px; }}
        .divider {{ {t["divider"]} margin: 15px 0; }}
        /* 评论区表情：小表情和大表情，垂直对齐文字基线 */
        .emote-small {{ display: inline; width: 22px; height: 22px; vertical-align: middle; margin: 0 1px; }}
        .emote-large {{ display: inline; width: 44px; height: 44px; vertical-align: middle; margin: 0 1px; }}
        /* 评论区图片容器：flex换行布局 */
        .images-container {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; justify-content: flex-start; }}
        /* 评论区图片：移动端兼容 max-width:100% 防溢出 */
        .comment-image {{ max-width: 100%; max-height: 280px; height: auto; object-fit: contain; border-radius: 6px; border: 1px solid #e0e0e0; }}
        /* B站跳转超链接 */
        .jump-link {{ color: #2196F3; text-decoration: none; }}
        .jump-link:hover {{ text-decoration: underline; }}
        /* 原帖截图容器 + 图片缩放（移动端 max-width:100% 防撑破容器） */
        .screenshot {{ text-align:center; margin-top:12px; }}
        .screenshot img {{ max-width:100%; width:100%; height:auto; border-radius:6px; box-shadow:0 2px 8px rgba(0,0,0,0.1); }}
        .post-context {{ background: #fdf6e3; border-left: 4px solid #e6a817; padding: 10px 14px; margin-bottom: 12px; border-radius: 4px; font-size: 13px; color: #555; }}
        .post-context .label {{ font-size: 11px; color: #b8860b; margin-bottom: 5px; font-weight: bold; }}
        .post-context .body {{ white-space: pre-wrap; word-break: break-word; line-height: 1.6; }}
        .post-video {{ margin-top: 10px; background: #f5f0e0; border-radius: 6px; padding: 10px; }}
        .post-video .video-body {{ display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }}
        .post-video .video-cover {{ max-width: 100%; max-height: 180px; height: auto; border-radius: 4px; border: 1px solid #ddd; }}
        .post-video .video-title {{ font-size: 13px; color: #333; font-weight: bold; }}
        .footer {{ text-align: center; color: #999; font-size: 11px; padding: 15px 15px 15px 15px; }}
        .footer-divider {{ height: 2px; background: {p}; background: linear-gradient(90deg, {p}, {s}); margin-bottom: 12px; }}
        .stats {{ background: #f9f9f9; padding: 12px; border-radius: 6px; margin-bottom: 15px; text-align: center; }}
        .stats span {{ margin: 0 10px; font-size: 13px; }}
    </style>
    """


def _embed_screenshot(screenshot_path: Optional[str]) -> Optional[str]:
    """
    将截图 PNG 文件读取为 base64 data URI，包裹在 .screenshot 容器中。

    照搬 BTCE3.0 方案：max-width:560px + width:100% 约束图片宽度，
    适配邮件容器（600px），2x Retina 截图自动缩放。

    Args:
        screenshot_path: 截图文件路径

    Returns:
        <div class="screenshot"><img ...></div> HTML 片段，失败返回 None
    """
    if not screenshot_path:
        return None
    try:
        with open(screenshot_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return (
            '<div class="screenshot">'
            f'<img src="data:image/png;base64,{b64}" '
            'style="max-width:100%;width:100%;height:auto;border-radius:6px;'
            'box-shadow:0 2px 8px rgba(0,0,0,0.1);" alt="原帖截图">'
            '</div>'
        )
    except Exception as e:
        logger.warning(f"嵌入截图失败 ({screenshot_path}): {e}")
        return None


def build_single_email(interaction: dict, up_name: str, item_type: str = "", theme: dict = None, post_content: str = "", post_rich_content: str = "", oid_map: dict = None) -> str:
    """构建单条互动邮件 HTML，可选传入预设主题（预览用）。场景二原帖自动从 sent_emails/ 读截图。"""
    if theme is None:
        theme = random_theme()
    item_id = interaction.get("item_id", "")
    rpid = interaction.get("comment_id", "")
    content = interaction.get("content", "")
    scene = interaction.get("scene", "scene1")
    is_sub = interaction.get("is_sub_reply", False)
    up_liked = interaction.get("up_liked", False)
    parent_content = interaction.get("parent_content", "")
    parent_author = interaction.get("parent_author", "")
    discovered_at = interaction.get("discovered_at", "")

    scene_name = "自身作品" if scene == "scene1" else ("话题互动" if scene == "scene2" else "切片视频")
    reply_type = "子评论" if is_sub else "主评论"
    # comment_oid（真实 aid）映射：视频评论用视频页链接，动态评论用动态页链接
    aid = (oid_map or {}).get(item_id, "")
    comment_url = build_comment_url(item_id, item_type, rpid, aid)
    item_url = build_item_url(item_id, item_type, aid)

    # 场景二/三：原帖上下文卡片（截图优先，文本降级）
    # 截图在入库时已生成，路径 = sent_emails/dynamic_{item_id}.png
    post_context_html = ""
    if scene in ("scene2", "scene3"):
        expected_shot = str(SENT_DIR / f"dynamic_{item_id}.png")
        screenshot_html = _embed_screenshot(expected_shot)
        if screenshot_html:
            # 截图模式：直接嵌入截图
            post_context_html = f"""
        <div class="post-context">
            <div class="label">原帖内容（截图）</div>
            <div class="body">{screenshot_html}</div>
        </div>"""
        elif post_content or post_rich_content:
            # 降级：文本+富内容渲染（表情+图片+视频卡片）
            body_html = render_comment_html(post_content[:500], post_rich_content)
            video_html = ""
            if post_rich_content:
                try:
                    rich = json.loads(post_rich_content)
                    video = rich.get("video", {})
                    if video.get("title"):
                        cover = _safe_url(video.get("cover", ""))
                        vid = video.get("aid", "") or video.get("bvid", "")
                        video_url = f"https://www.bilibili.com/video/av{vid}" if vid else ""
                        video_html = f"""
                    <div class="post-video">
                        <div class="label">原帖视频</div>
                        <div class="video-body">
                            {f'<img class="video-cover" src="{html.escape(cover)}" alt="视频封面">' if cover else ''}
                            <div class="video-title">{html.escape(video['title'])}</div>
                            {f'<a class="btn" href="{html.escape(video_url)}" target="_blank" style="font-size:11px;margin-top:6px">观看视频</a>' if video_url else ''}
                        </div>
                    </div>"""
                except (json.JSONDecodeError, TypeError):
                    pass
            post_context_html = f"""
        <div class="post-context">
            <div class="label">原帖内容</div>
            <div class="body">{body_html}</div>
            {video_html}
        </div>"""

    context_html = ""
    if is_sub and parent_author:
        # 被回复内容也用富内容渲染（表情包/图片），parent_rich_content 为空时退化为纯文本
        parent_html = render_comment_html(parent_content, interaction.get("parent_rich_content"))
        context_html = f"""
        <div class="context-box">
            <strong>{html.escape(parent_author)}</strong>：{parent_html}
        </div>"""

    like_html = ""
    if up_liked:
        like_html = '<span class="badge">UP觉得很赞</span>'

    # 渲染评论内容（文字+表情+图片），前加【UP名】标签便于识别互动来源
    safe_up = html.escape(up_name)
    content_html = render_comment_html(
        content, interaction.get("rich_content")
    )
    up_tag = f'<span class="badge">【{safe_up}】</span>'

    trace = f"cid={rpid} | scene={scene} | {theme['primary']}→{theme['secondary']}"
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">{_base_style(theme)}</head>
<body>
<div class="container">
    <div class="header">
        <h1>EchoWatch · 留声</h1>
        <p>留住「{safe_up}」的真心与回响</p>
    </div>
    <div class="content">
        <div class="stats">
            <span>UP主：<strong>{safe_up}</strong></span>
            <span>场景：{scene_name}</span>
            <span>时间：{html.escape(discovered_at)}</span>
        </div>
        <div class="interaction">
            <span class="badge">{reply_type}</span>{like_html}
            {post_context_html}
            {context_html}
            <div class="text">{up_tag}{content_html}</div>
            <div style="margin-top:12px">
                <a class="btn" href="{html.escape(comment_url)}" target="_blank">评论直达</a>
                <a class="btn" href="{html.escape(item_url)}" target="_blank" style="opacity:0.8">作品页面</a>
            </div>
        </div>
    </div>
    <div class="footer">
        <div class="footer-divider"></div>
        EchoWatch · 留住「{safe_up}」的真心与回响<br>
        此邮件由自动化系统发送<br>
        <span style="font-size:10px;color:#aaa;">追踪: {html.escape(trace)}</span>
    </div>
</div>
</body></html>"""


def build_digest_email(interactions: list, up_name: str = "", post_contents: dict = None, post_rich_contents: dict = None, title: str = None, today_count: int = None, oid_map: dict = None) -> str:
    """
    构建汇总邮件 HTML（日报 + 场景二批量共用）。

    Args:
        interactions: 互动记录列表
        up_name: UP主名称
        post_contents: item_id → post_content 映射（场景二原帖正文，可选）
        post_rich_contents: item_id → post_rich_content 映射（可选）
        title: 自定义标题（不传则用日期+日报默认标题）
        oid_map: item_id → comment_oid 映射（视频评论链接用真实 aid）
    """
    theme = random_theme()
    today_str = datetime.now().strftime("%Y年%m月%d日")
    pc_map = post_contents or {}
    pr_map = post_rich_contents or {}
    oid_map = oid_map or {}
    header_title = title or f"{today_str} UP主互动日报"

    items_html = ""
    for i, interaction in enumerate(interactions):
        item_id = interaction.get("item_id", "")
        rpid = interaction.get("comment_id", "")
        content = interaction.get("content", "")
        scene = interaction.get("scene", "scene1")
        is_sub = interaction.get("is_sub_reply", False)
        up_liked = interaction.get("up_liked", False)
        parent_content = interaction.get("parent_content", "")
        parent_author = interaction.get("parent_author", "")
        discovered_at = interaction.get("discovered_at", "")

        scene_name = "自身作品" if scene == "scene1" else ("话题互动" if scene == "scene2" else "切片视频")
        reply_type = "子评论" if is_sub else "主评论"
        # comment_oid（真实 aid）映射：视频评论用视频页链接（场景三/视频动态必须，动态ID≠aid）
        comment_url = build_comment_url(item_id, "", rpid, oid_map.get(item_id, ""))

        # 场景二/三：原帖上下文卡片（截图优先，文本降级）
        # 截图在入库时已生成，路径 = sent_emails/dynamic_{item_id}.png
        post_context_html = ""
        if scene in ("scene2", "scene3"):
            expected_shot = str(SENT_DIR / f"dynamic_{item_id}.png")
            screenshot_html = _embed_screenshot(expected_shot)
            if screenshot_html:
                # 截图模式：直接嵌入截图
                post_context_html = '<div class="post-context"><div class="label">原帖内容（截图）</div><div class="body">' + screenshot_html + '</div></div>'
            else:
                # 降级：文本+富内容渲染
                pc = pc_map.get(item_id, "")
                pr = pr_map.get(item_id, "")
                if pc or pr:
                    body_html = render_comment_html(pc[:500] if pc else "", pr)
                    video_html = ""
                    if pr:
                        try:
                            rich = json.loads(pr)
                            video = rich.get("video", {})
                            if video.get("title"):
                                cover = _safe_url(video.get("cover", ""))
                                vid = video.get("aid", "") or video.get("bvid", "")
                                video_url = "https://www.bilibili.com/video/av" + vid if vid else ""
                                video_html = (
                                    '<div class="post-video">'
                                    '<div class="label">原帖视频</div>'
                                    '<div class="video-body">'
                                    + ('<img class="video-cover" src="' + html.escape(cover) + '" alt="视频封面">' if cover else '')
                                    + '<div class="video-title">' + html.escape(video["title"]) + '</div>'
                                    + ('<a class="btn" href="' + html.escape(video_url) + '" target="_blank" style="font-size:11px;margin-top:6px">观看视频</a>' if video_url else '')
                                    + '</div></div>'
                                )
                        except (json.JSONDecodeError, TypeError):
                            pass
                    post_context_html = '<div class="post-context"><div class="label">原帖内容</div><div class="body">' + body_html + '</div>' + video_html + '</div>'

        context_html = ""
        if is_sub and parent_author:
            # 被回复内容也用富内容渲染（表情包/图片），parent_rich_content 为空时退化为纯文本
            parent_html = render_comment_html(parent_content, interaction.get("parent_rich_content"))
            context_html = f'<div class="context-box"><strong>{html.escape(parent_author)}</strong>：{parent_html}</div>'

        like_badge = '<span class="badge">UP觉得很赞</span>' if up_liked else ""
        scene_badge = f'<span class="badge">{scene_name}</span>'
        # 主评论/子评论也打徽章（与单封邮件一致）
        reply_badge = f'<span class="badge">{reply_type}</span>'

        # 渲染评论内容（文字+表情+图片），前加【UP名】标签便于识别互动来源
        up_tag = f'<span class="badge">【{html.escape(up_name)}】</span>'
        content_html = render_comment_html(
            content, interaction.get("rich_content")
        )

        items_html += f"""
        <div class="interaction">
            <div class="label" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px">
                <span>#{i+1} {html.escape(discovered_at)} {scene_badge} {like_badge} {reply_badge}</span>
                <a class="btn" href="{html.escape(comment_url)}" target="_blank" style="padding:3px 12px; font-size:11px; margin-right:0">评论直达</a>
            </div>
            {post_context_html}
            {context_html}
            <div class="text">{up_tag}{content_html}</div>
        </div>"""

    # 追踪信息：批量邮件补上 cid（场景三无单封邮件，靠此定位评论）
    trace = f"{today_str} · 共{len(interactions)}条"
    if interactions:
        trace += f" | 首条cid={interactions[0].get('comment_id', '')}"
    trace += f" | {theme['primary']}→{theme['secondary']}"
    safe_up = html.escape(up_name)

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">{_base_style(theme)}</head>
<body>
<div class="container">
    <div class="header">
        <h1>EchoWatch · 留声</h1>
        <p>{html.escape(header_title)}</p>
    </div>
    <div class="content">
        <div class="stats">
            <span>UP主：<strong>{safe_up}</strong></span>
            <span>本轮互动：<strong>{len(interactions)} 条</strong></span>
            {f'<span>今日累计：<strong>{today_count} 条</strong></span>' if today_count is not None else ''}
        </div>
        {items_html}
    </div>
    <div class="footer">
        <div class="footer-divider"></div>
        EchoWatch · 留住「{safe_up}」的真心与回响<br>
        此邮件由自动化系统发送<br>
        <span style="font-size:10px;color:#aaa;">追踪: {html.escape(trace)}</span>
    </div>
</div>
</body></html>"""


# ============================================================
# 邮件留档
# ============================================================

SENT_DIR = Path(__file__).parent / "sent_emails"


def _archive_email(filename: str, html: str):
    """将已发送的邮件HTML保存到 sent_emails/ 目录留档"""
    try:
        SENT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = SENT_DIR / _safe_filename(filename)
        filepath.write_text(html, encoding="utf-8")
        logger.info(f"邮件已留档: {filename}")
    except Exception as e:
        logger.warning(f"邮件留档失败: {e}")


# ============================================================
# SMTP 发送
# ============================================================

def _send_email_sync(subject: str, html: str, config) -> bool:
    """
    同步发送邮件（由 asyncio.to_thread 包装）。
    参考 BTCE3.0 email_utils.py 的实现。
    """
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = config.email.sender_email
    msg["To"] = config.email.receiver_email

    try:
        if config.email.use_tls:
            with smtplib.SMTP_SSL(
                config.email.smtp_server, config.email.smtp_port, timeout=30
            ) as server:
                server.login(config.email.sender_email, config.email.sender_password)
                server.sendmail(
                    config.email.sender_email,
                    [config.email.receiver_email],
                    msg.as_string(),
                )
        else:
            with smtplib.SMTP(
                config.email.smtp_server, config.email.smtp_port, timeout=30
            ) as server:
                server.starttls()
                server.login(config.email.sender_email, config.email.sender_password)
                server.sendmail(
                    config.email.sender_email,
                    [config.email.receiver_email],
                    msg.as_string(),
                )
        return True
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"邮件认证失败: {e}")
        return False
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


# ============================================================
# Notifier 类
# ============================================================

class Notifier:
    """邮件通知管理"""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    async def send_immediate(self, interaction: dict, up_name: str, item_type: str = "", is_priority: bool = False):
        """即时发送单条互动邮件（is_priority 用"置顶动态有更新"标题）"""
        if not self.config.notify.immediate:
            return

        # 场景二/三：查原帖正文和富内容（截图在入库时已生成，builder 自动读取）
        post_content = ""
        post_rich = ""
        oid_map = {}
        if interaction.get("scene") in ("scene2", "scene3"):
            item_id = interaction.get("item_id", "")
            post_content = await self.db.get_item_post_content(item_id)
            post_rich = await self.db.get_item_post_rich_content(item_id)
        # comment_oid 映射：视频评论链接用真实 aid（所有场景都查，动态ID≠aid）
        item_id = interaction.get("item_id", "")
        if item_id:
            oid_map[item_id] = await self.db.get_item_comment_oid(item_id)

        if is_priority:
            subject = f"【EchoWatch】UP主「{up_name}」置顶动态有更新"
        else:
            subject = f"【EchoWatch】UP主「{up_name}」有新互动"
        html = build_single_email(interaction, up_name, item_type, post_content=post_content, post_rich_content=post_rich, oid_map=oid_map)

        result = await asyncio.to_thread(_send_email_sync, subject, html, self.config)
        if result:
            await self.db.mark_notified_immediate([interaction["id"]])
            cid = interaction.get("comment_id", "")[:30]
            logger.info(f"即时邮件已发送: comment_id={cid}...")
            # 留档
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _archive_email(f"{ts}_immediate_{cid}.html", html)
        else:
            logger.warning("即时邮件发送失败")

    async def send_pinned_change(self, up_name: str, up_uid: str,
                                  new_id: Optional[str], old_id: Optional[str],
                                  is_new: bool):
        """
        发送置顶动态变更邮件通知。

        Args:
            up_name: UP主 昵称
            up_uid: UP主 UID
            new_id: 新的置顶动态ID（None表示置顶已消失）
            old_id: 旧的置顶动态ID
            is_new: 是否是首次发现
        """
        if not self.config.notify.immediate:
            return

        # 构建邮件内容
        if new_id is None:
            title_text = "置顶动态已取消"
            desc_text = f"UP主「{up_name}」的置顶动态已取消（原ID: {old_id}）"
        elif is_new:
            title_text = "首次发现置顶动态"
            desc_text = f"已自动发现 UP主「{up_name}」的置顶动态"
        else:
            title_text = "置顶动态已更换"
            desc_text = f"UP主「{up_name}」的置顶动态已更换"

        theme = random_theme()
        new_link = f"https://t.bilibili.com/{new_id}" if new_id else ""
        old_link = f"https://t.bilibili.com/{old_id}" if old_id else ""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f5f5f5;font-family:'Microsoft YaHei',sans-serif;">
<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;
            box-shadow:{theme['shadow']};">
  <div style="background:linear-gradient(135deg,{theme['primary']},{theme['secondary']});
              {theme['texture']};padding:28px 24px;text-align:center;">
    <h1 style="color:#fff;margin:0;font-size:20px;text-shadow:0 1px 3px rgba(0,0,0,0.2);">
      {html.escape(title_text)}
    </h1>
  </div>
  <div style="padding:24px;">
    <p style="font-size:15px;color:#333;line-height:1.8;margin:0 0 16px;">
      {html.escape(desc_text)}
    </p>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr>
        <td style="padding:10px 12px;background:#f9f9f9;color:#666;border-radius:6px 0 0 6px;width:80px;">
          UP主
        </td>
        <td style="padding:10px 12px;">{html.escape(up_name)} (UID:{html.escape(up_uid)})</td>
      </tr>"""

        if old_id:
            html += f"""
      <tr>
        <td style="padding:10px 12px;background:#f9f9f9;color:#666;">旧置顶</td>
        <td style="padding:10px 12px;">
          <a href="{html.escape(old_link)}" style="color:{theme['primary']};">{html.escape(old_id)}</a>
        </td>
      </tr>"""

        if new_id:
            html += f"""
      <tr>
        <td style="padding:10px 12px;background:#f9f9f9;color:#666;">新置顶</td>
        <td style="padding:10px 12px;">
          <a href="{html.escape(new_link)}" style="color:{theme['primary']};font-weight:bold;">{html.escape(new_id)}</a>
        </td>
      </tr>"""

        html += f"""
    </table>
    <p style="font-size:12px;color:#999;margin-top:20px;text-align:center;">
      系统已自动更新 Priority 监测目标，新置顶将纳入高频轮询
    </p>
  </div>
</div>
</body>
</html>"""

        subject = f"【EchoWatch】UP主「{up_name}」{title_text}"
        result = await asyncio.to_thread(_send_email_sync, subject, html, self.config)
        if result:
            logger.info(f"置顶变更邮件已发送: {up_name} old={old_id} new={new_id}")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _archive_email(f"{ts}_pinned_change_{up_uid}.html", html)
        else:
            logger.warning(f"置顶变更邮件发送失败: {up_name}")

    async def send_scene2_batch(self, up_name: str = ""):
        """场景二批量发送：将所有待通知的场景二互动合并为一封邮件"""
        if not self.config.notify.immediate:
            return

        interactions = await self.db.get_unnotified_immediate(scene="scene2")
        if not interactions:
            return

        # 批量查原帖正文和富内容
        post_contents = {}
        post_rich_map = {}
        oid_map = {}
        for it in interactions:
            item_id = it.get("item_id", "")
            if item_id and item_id not in post_contents:
                pc = await self.db.get_item_post_content(item_id)
                pr = await self.db.get_item_post_rich_content(item_id)
                if pc:
                    post_contents[item_id] = pc
                if pr:
                    post_rich_map[item_id] = pr
                oid_map[item_id] = await self.db.get_item_comment_oid(item_id)

        now_str = datetime.now().strftime("%H:%M")
        subject = f"【EchoWatch】UP主「{up_name}」话题互动汇总（{len(interactions)}条）"

        # 今日累计：按本批涉及的 UP 分别查库求和（今日的应该累加，本轮条数只是本次发送量）
        today_count = 0
        for uid in {it.get("up_uid", "") for it in interactions if it.get("up_uid")}:
            today_count += len(await self.db.get_today_interactions(uid))

        html = build_digest_email(interactions, up_name, post_contents,
                                  post_rich_contents=post_rich_map,
                                  title=f"话题互动即时汇总 ({now_str})",
                                  today_count=today_count, oid_map=oid_map)

        result = await asyncio.to_thread(_send_email_sync, subject, html, self.config)
        if result:
            ids = [i["id"] for i in interactions]
            await self.db.mark_notified_immediate(ids)
            logger.info(f"场景二批量邮件已发送: {len(interactions)} 条互动")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _archive_email(f"{ts}_scene2_batch_{len(interactions)}条.html", html)
        else:
            logger.error("场景二批量邮件发送失败")

    async def send_scene3_batch(self, up_name: str = ""):
        """场景三批量发送：将所有待通知的场景三互动（切片视频下目标UP主回复）合并为一封邮件"""
        if not self.config.notify.immediate:
            return

        interactions = await self.db.get_unnotified_immediate(scene="scene3")
        if not interactions:
            return

        # 批量查原帖正文和富内容（切片视频标题/封面，邮件上下文用）
        post_contents = {}
        post_rich_map = {}
        oid_map = {}
        for it in interactions:
            item_id = it.get("item_id", "")
            if item_id and item_id not in post_contents:
                pc = await self.db.get_item_post_content(item_id)
                pr = await self.db.get_item_post_rich_content(item_id)
                if pc:
                    post_contents[item_id] = pc
                if pr:
                    post_rich_map[item_id] = pr
                oid_map[item_id] = await self.db.get_item_comment_oid(item_id)

        now_str = datetime.now().strftime("%H:%M")
        subject = f"【EchoWatch】UP主「{up_name}」切片视频互动汇总（{len(interactions)}条）"

        # 今日累计：按本批涉及的 UP 分别查库求和（今日的应该累加，本轮条数只是本次发送量）
        today_count = 0
        for uid in {it.get("up_uid", "") for it in interactions if it.get("up_uid")}:
            today_count += len(await self.db.get_today_interactions(uid))

        html = build_digest_email(interactions, up_name, post_contents,
                                  post_rich_contents=post_rich_map,
                                  title=f"切片视频互动即时汇总 ({now_str})",
                                  today_count=today_count, oid_map=oid_map)

        result = await asyncio.to_thread(_send_email_sync, subject, html, self.config)
        if result:
            ids = [i["id"] for i in interactions]
            await self.db.mark_notified_immediate(ids)
            logger.info(f"场景三批量邮件已发送: {len(interactions)} 条互动")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _archive_email(f"{ts}_scene3_batch_{len(interactions)}条.html", html)
        else:
            logger.error("场景三批量邮件发送失败")

    async def send_priority_sub_batch(self, up_name: str = "", up_uid: str = None):
        """
        priority 子评论批量发送：合并为一封邮件（复用场景二的汇总模板）。

        规则（XTong 2026-08-14 定）：priority 主评论即时推送；
        子评论不即时，由调度循环每 priority_sub_batch_seconds 合并一封；
        日报照常汇总（本方法只标记 notified_immediate，不动 notified_digest）。
        """
        if not self.config.notify.immediate:
            return

        interactions = await self.db.get_unnotified_immediate(scene="scene1")
        if not interactions:
            return

        # 只取 priority 项的子评论（主评论由 _poll_item 即时发送）
        priority_ids = await self.db.get_priority_item_ids()
        subs = [
            it for it in interactions
            if it.get("is_sub_reply") and it.get("item_id", "") in priority_ids
            and (up_uid is None or it.get("up_uid") == up_uid)
        ]
        if not subs:
            return

        now_str = datetime.now().strftime("%H:%M")
        subject = f"【EchoWatch】UP主「{up_name}」置顶动态子评论汇总（{len(subs)}条）"

        # 今日累计：按本批涉及的 UP 分别查库求和
        today_count = 0
        for uid in {it.get("up_uid", "") for it in subs if it.get("up_uid")}:
            today_count += len(await self.db.get_today_interactions(uid))

        # comment_oid 映射：priority 视频动态的评论链接用真实 aid
        oid_map = {}
        for it in subs:
            item_id = it.get("item_id", "")
            if item_id and item_id not in oid_map:
                oid_map[item_id] = await self.db.get_item_comment_oid(item_id)

        # 复用场景二同款汇总模板
        html = build_digest_email(
            subs, up_name,
            title=f"置顶动态子评论汇总 ({now_str})",
            today_count=today_count,
            oid_map=oid_map,
        )

        result = await asyncio.to_thread(_send_email_sync, subject, html, self.config)
        if result:
            ids = [i["id"] for i in subs]
            await self.db.mark_notified_immediate(ids)
            logger.info(f"priority子评论批量邮件已发送: {len(subs)} 条互动")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _archive_email(f"{ts}_priority_sub_batch_{len(subs)}条.html", html)
        else:
            logger.error("priority子评论批量邮件发送失败")

    async def send_daily_digest(self, up_name: str = "", up_uid: str = None):
        """发送日报：汇总今天所有未入日报的互动"""
        if not self.config.notify.daily_digest:
            return

        interactions = await self.db.get_unnotified_digest(up_uid)
        if not interactions:
            logger.info("日报：今日无新互动，跳过")
            return

        # 场景二/三：批量查原帖正文和富内容（item_id → 内容 映射）
        post_contents = {}
        post_rich_map = {}
        oid_map = {}
        for it in interactions:
            item_id = it.get("item_id", "")
            if item_id and item_id not in post_contents:
                pc = await self.db.get_item_post_content(item_id)
                pr = await self.db.get_item_post_rich_content(item_id)
                if pc:
                    post_contents[item_id] = pc
                if pr:
                    post_rich_map[item_id] = pr
                # 所有场景都查 comment_oid（视频评论链接用真实 aid）
                oid_map[item_id] = await self.db.get_item_comment_oid(item_id)

        subject = f"【EchoWatch·日报】{datetime.now().strftime('%m月%d日')} UP主互动汇总（{len(interactions)}条）"

        # 今日累计：按 UP 查库（含已入日报的，体现全天总量）
        today_count = len(await self.db.get_today_interactions(up_uid)) if up_uid else len(interactions)

        html = build_digest_email(interactions, up_name, post_contents,
                                  post_rich_contents=post_rich_map,
                                  today_count=today_count, oid_map=oid_map)

        result = await asyncio.to_thread(_send_email_sync, subject, html, self.config)
        if result:
            ids = [i["id"] for i in interactions]
            await self.db.mark_notified_digest(ids)
            logger.info(f"日报已发送: {len(interactions)} 条互动")
            # 留档
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _archive_email(f"{ts}_digest_{len(interactions)}条.html", html)
        else:
            logger.error("日报发送失败")


# ============================================================
# 快速测试入口
# ============================================================

async def _test():
    """快速验证邮件模块"""
    from config import Config

    cfg = Config("config.example.yaml")
    db = await Database(cfg.database.path).initialize()

    notifier = Notifier(cfg, db)

    # 富内容测试数据
    test_rich = json.dumps({
        "emote": {
            "[表情包_示例]": {
                "url": "//i0.hdslb.com/bfs/emote/487390494869c70391cc5744f6cae569e153efef.png",
                "meta": {"size": 1},
            },
        },
        "pictures": [],
        "jump_url": {},
    }, ensure_ascii=False)

    # 构造一条假互动测试模板渲染
    test_interaction = {
        "id": 999,
        "up_uid": "000000000",
        "item_id": "999888777000111222",
        "comment_id": "111222333444",
        "is_sub_reply": False,
        "parent_content": "",
        "parent_author": "",
        "content": "这是一条测试评论内容 [表情包_示例]",
        "rich_content": test_rich,
        "up_liked": False,
        "scene": "scene1",
        "discovered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    html = build_single_email(test_interaction, "某UP主")
    print(f"[OK] 单封邮件 HTML 生成: {len(html)} 字符")

    # 测试子评论（含上下文）
    test_sub = {
        "id": 1000,
        "up_uid": "000000000",
        "item_id": "999888777000111222",
        "comment_id": "111222333555",
        "is_sub_reply": True,
        "parent_content": "UP主今天好可爱！",
        "parent_author": "粉丝小A",
        "content": "谢谢支持~ [表情包_害羞]",
        "rich_content": json.dumps({
            "emote": {
                "[表情包_害羞]": {
                    "url": "//i0.hdslb.com/bfs/emote/3087f9a56ca1df91e2d5f85fd5e4a1af8d5d8c70.png",
                    "meta": {"size": 2},
                },
            },
            "pictures": [],
            "jump_url": {},
        }, ensure_ascii=False),
        "up_liked": False,
        "scene": "scene1",
        "discovered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    html2 = build_single_email(test_sub, "某UP主")
    print(f"[OK] 子评论邮件 HTML 生成: {len(html2)} 字符")

    # 测试日报
    html3 = build_digest_email([test_interaction, test_sub], "某UP主")
    print(f"[OK] 日报 HTML 生成: {len(html3)} 字符")

    # 测试随机主题
    themes = [random_theme() for _ in range(3)]
    for t in themes:
        print(f"[OK] 氛围={t['mood_name']} 主色={t['primary']} 副色={t['secondary']} 圆角={t['radius']}px")

    # 测试降级：rich_content 为空
    test_no_rich = {
        "id": 1001,
        "up_uid": "000000000",
        "item_id": "999888777000111222",
        "comment_id": "309143024561",
        "is_sub_reply": False,
        "parent_content": "",
        "parent_author": "",
        "content": "旧数据评论 <无富内容>",
        "rich_content": None,
        "up_liked": False,
        "scene": "scene1",
        "discovered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    html4 = build_single_email(test_no_rich, "某UP主")
    print(f"[OK] 降级邮件 HTML 生成: {len(html4)} 字符")

    await db.close()
    print("\n[OK] 通知模块测试通过（注：未实际发送邮件）")


if __name__ == "__main__":
    asyncio.run(_test())
