# -*- coding: utf-8 -*-
"""子评论上下文工具 —— 用本地快照补全「被回复的原评论」

背景（2026-11-16 排查结论）：
B站子评论接口只返回 parent 的 rpid，不返回父评论正文。原实现只能从
本次翻页窗口里查找父评论，而稳态翻页是尾部窗口（只回看约 2 页），
窗口外的老父评论在邮件里就成了空白 —— 表现为「有的回复带原评论、
有的不带」。

解决方式：每次翻页拿到的子评论顺手写入本地快照表（零额外 API 请求），
解析父评论时按「根评论上下文 → 本次窗口 → 本地快照」依次回退。
"""

from bili_client import parse_sub_comment


def parse_and_index(all_subs: list):
    """解析窗口内子评论，并构建 rpid → 上下文 查找表。

    原来每个元素要 parse_sub_comment 两次（建表一次、遍历一次），
    这里只解析一次，顺带省一点弱性能服务器的 CPU。

    Args:
        all_subs: B站子评论接口返回的原始列表

    Returns:
        (parsed_subs, sub_lookup)
        parsed_subs: parse_sub_comment 解析后的列表
        sub_lookup: {str(rpid): {"uname", "content", "rich_content"}}

    注意：key 统一用 str(rpid)。DB 的 comment_id/rpid 都是 TEXT，
    混用 int/str 会导致查不到父评论。
    """
    parsed_subs = [parse_sub_comment(raw) for raw in all_subs]
    sub_lookup = {}
    for parsed in parsed_subs:
        sub_lookup[str(parsed["rpid"])] = {
            "uname": parsed["uname"],
            "content": parsed["content"],
            "rich_content": parsed.get("rich_content", ""),
        }
    return parsed_subs, sub_lookup


async def save_snapshots(db, parsed_subs: list, item_id: str, root_rpid) -> None:
    """把本次翻页拿到的子评论快照批量入库（INSERT OR IGNORE）。"""
    await db.save_sub_comment_snapshots(parsed_subs, item_id, root_rpid)


async def load_missing_parents(db, parsed_subs: list, root_rpid,
                               up_uid: str, sub_lookup: dict) -> dict:
    """只查「UP主回复中、父评论不在本次窗口」的那部分，批量走本地快照表。

    先过滤再查库：一轮通常只有几条 UP 主回复，避免把整个窗口的 rpid
    都塞进 IN 查询。

    Args:
        db: Database 实例
        parsed_subs: 本次窗口解析后的子评论
        root_rpid: 根评论 rpid
        up_uid: 目标 UP主 UID（只关心他的回复）
        sub_lookup: 本次窗口的 rpid 查找表

    Returns:
        {str(parent_rpid): {"uname", "content", "rich_content"}}
    """
    missing = set()
    root_key = str(root_rpid)
    for parsed in parsed_subs:
        if parsed["mid"] != up_uid:
            continue
        key = str(parsed["parent_rpid"])
        if key == root_key or key in sub_lookup:
            continue
        missing.add(key)
    if not missing:
        return {}
    return await db.get_sub_comment_snapshots(sorted(missing))


def pick_parent(parsed: dict, root_rpid, root_context: dict,
                sub_lookup: dict, cached_parents: dict):
    """解析一条子评论被回复对象的内容，按优先级回退。

    顺序：被回复的是根评论 → 本次窗口内 → 本地快照 → 全部留空。

    Args:
        parsed: parse_sub_comment 解析后的子评论
        root_rpid: 根评论 rpid
        root_context: 主列表轮询带下来的根评论上下文（sweep 场景为 None）
        sub_lookup: 本次窗口的 rpid 查找表
        cached_parents: 本地快照查到的父评论映射

    Returns:
        (parent_content, parent_author, parent_rich_content)，都找不到返回空串
    """
    key = str(parsed["parent_rpid"])
    if key == str(root_rpid):
        if root_context:
            return (
                root_context.get("content", ""),
                root_context.get("uname", ""),
                root_context.get("rich_content", ""),
            )
        return "", "", ""

    info = sub_lookup.get(key) or cached_parents.get(key)
    if info:
        return (
            info.get("content", ""),
            info.get("uname", ""),
            info.get("rich_content", ""),
        )
    return "", "", ""
