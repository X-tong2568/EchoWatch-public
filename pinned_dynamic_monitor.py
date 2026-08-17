# pinned_dynamic_monitor.py
"""EchoWatch 置顶动态自动检测模块 —— 通过 space feed API 自动发现置顶动态，检测更换。

参照 BTCE3.0 pinned_dynamic_monitor.py 的设计思路，但改用 EchoWatch 的 BiliClient
（零Cookie + WBI签名，无需登录态）。本项目全自动模式，不区分手动/自动。

核心流程：
1. 调用 space feed API 获取动态列表
2. 遍历 items，检查 modules.module_tag.text == "置顶"
3. 与本地状态文件对比，检测变更
4. 变更时更新状态文件，由调用方（scheduler）负责发通知
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

import yaml

from logger_config import logger

# 状态文件路径（与 config.yaml 同目录）
STATE_FILE = Path(__file__).parent / "pinned_dynamic_state.json"


# ============================================================
# 状态文件读写
# ============================================================

def _load_state() -> dict:
    """加载本地状态文件，不存在或损坏则返回空字典"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    """保存状态到本地 JSON 文件"""
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_current_pinned_id() -> Optional[str]:
    """从状态文件读取当前追踪的置顶动态ID"""
    return _load_state().get("pinned_dynamic_id")


# ============================================================
# config.yaml 同步
# ============================================================

def sync_pinned_id_to_config(uid: str, pinned_id: Optional[str], config_path: str = "config.yaml"):
    """
    将自动发现的置顶ID写回 config.yaml，保证重启后兜底可用。

    仅更新匹配 uid 的 UP主 的 priority_dynamics 列表第一个元素。
    如果该 UP主 在 config 中不存在，不做任何修改。

    Args:
        uid: B站 UID
        pinned_id: 新的置顶动态ID（None 表示清除）
        config_path: 配置文件路径
    """
    file_path = Path(config_path)
    if not file_path.exists():
        logger.warning(f"config.yaml 不存在，跳过写入: {config_path}")
        return

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"读取 config.yaml 失败: {e}")
        return

    up_list = config_data.get("up_list", [])
    updated = False
    for up_entry in up_list:
        if str(up_entry.get("uid", "")) == str(uid):
            if pinned_id:
                up_entry["priority_dynamics"] = [str(pinned_id)]
            else:
                up_entry["priority_dynamics"] = []
            updated = True
            logger.info(f"config.yaml 已更新: uid={uid} priority_dynamics={up_entry['priority_dynamics']}")
            break

    if not updated:
        logger.debug(f"config.yaml 中未找到 uid={uid}，跳过写入")
        return

    # 原子写入：先写临时文件再 os.replace，避免写一半崩溃留下截断的配置文件
    tmp_path = file_path.with_name(file_path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, file_path)
        logger.info(f"config.yaml 写入成功")
    except Exception as e:
        logger.warning(f"写入 config.yaml 失败: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# 核心检测逻辑
# ============================================================

async def check_pinned_dynamic(client, uid: str) -> dict:
    """
    通过 space feed API 发现当前置顶动态，检测是否更换。

    使用 EchoWatch 的 BiliClient（零Cookie + WBI签名）。

    Args:
        client: BiliClient 实例
        uid: B站 UID

    Returns:
        {
            "changed": bool,        # 是否发生变更（首次发现也算）
            "current_id": str|None, # 当前应监控的置顶ID
            "previous_id": str|None,# 之前监控的置顶ID
            "is_new": bool,         # 是否是首次发现（之前无置顶）
            "api_id": str|None,     # API 实际发现的置顶ID
        }
    """
    result = {
        "changed": False,
        "current_id": None,
        "previous_id": None,
        "is_new": False,
        "api_id": None,
    }

    # 获取 B站 当前置顶动态ID
    # API 失败（风控/网络）时跳过本轮：绝不能当作"置顶消失"处理，否则会误清状态+误发通知
    try:
        api_pinned_id = await client.get_pinned_dynamic_id(uid)
    except Exception as e:
        logger.warning(f"置顶检测API失败，本轮跳过（避免误判置顶消失）: {e}")
        return result
    state = _load_state()
    monitored_id = state.get("pinned_dynamic_id")

    result["api_id"] = api_pinned_id
    result["current_id"] = monitored_id or api_pinned_id
    result["previous_id"] = monitored_id

    # 情况1：API 未检测到置顶标记
    # 注意：space feed 的置顶标签可能间歇性漏检（风控/缓存/接口改版），
    # 返回 None 并不代表置顶真的被取消。因此只有检测到"新的置顶ID"才允许
    # 替换旧置顶（新替旧），None 绝不替换 —— 防止"空白替换旧置顶"导致
    # 误清 priority、误发取消邮件。真实取消置顶的场景不单独通知。
    if api_pinned_id is None:
        if monitored_id:
            logger.warning(
                f"置顶监测: API 未检测到置顶标记 (上次: {monitored_id})，"
                f"保持现状不清理（等待新置顶出现后再替换）"
            )
            result["current_id"] = monitored_id
        return result

    # 情况2：无变化
    if api_pinned_id == monitored_id:
        return result

    # 情况3：有变化（首次发现 或 自动更换）
    is_new = (monitored_id is None)
    logger.info(
        f"置顶监测: {'首次发现' if is_new else '已更换'} 置顶动态 -> {api_pinned_id}"
    )

    # 更新状态文件
    state["pinned_dynamic_id"] = api_pinned_id
    state["last_change"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state(state)

    result["changed"] = True
    result["is_new"] = is_new
    result["current_id"] = api_pinned_id
    return result


# ============================================================
# 独立测试入口
# ============================================================

if __name__ == "__main__":
    import asyncio
    from bili_client import BiliClient
    from config import Config

    async def main():
        cfg = Config("config.yaml")
        client = BiliClient()
        try:
            for up in cfg.up_list:
                print(f"\n=== 检测 {up.name} (UID={up.uid}) 的置顶动态 ===")
                result = await check_pinned_dynamic(client, up.uid)
                if result["changed"]:
                    print(
                        f"[变更] {'首次发现' if result['is_new'] else '已更换'}\n"
                        f"  新: {result['current_id']}\n"
                        f"  旧: {result['previous_id']}"
                    )
                else:
                    print(f"[无变更] 当前={result['current_id']}")
        finally:
            await client.close()

    asyncio.run(main())
