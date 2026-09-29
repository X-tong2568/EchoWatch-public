# B站不登录获取评论 API 实现指南

核心目标：**不登录、不用 Cookie、不需要浏览器，纯 HTTP 请求获取 B站动态页的评论数据（含 rpid）。**

## 涉及 API 总览

| API | 用途 | 需要签名 | 需要 Cookie |
|-----|------|---------|------------|
| `/x/polymer/web-dynamic/v1/detail` | 拿动态详情（oid + comment_type） | 不需要 | 不需要 |
| `/x/web-interface/nav` | 拿 WBI 密钥 | 不需要 | 不需要 |
| `/x/v2/reply/wbi/main` | 拿主评论列表 | ✅ WBI | 不需要 |
| `/x/v2/reply/reply` | 拿子评论（楼中楼） | 不需要 | 不需要 |

> 注：B站还有 `get_comments_lazy` 等老版 API（需 Cookie），本文只用公开 API。

---

## API 1：获取动态详情

### 作用

拿到动态对应的**评论区线程ID（oid）**和**评论区类型（comment_type）**。这两个值是调评论 API 的必传参数。

### 请求

```
GET https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dynamic_id}
```

- 方法：GET
- 参数：`id` — 动态ID，即 `t.bilibili.com/{id}` 末尾数字
- 无签名、无 Cookie、Referer 不需要

curl 示例：
```bash
curl -s "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id=999888777000111222" | python3 -m json.tool
```

### 完整响应结构

```json
{
  "code": 0,
  "message": "0",
  "data": {
    "item": {
      "basic": {
        "comment_id_str": "999888777",   // ← oid，评论区线程ID（字符串）
        "comment_type": 11,               // ← 评论区类型
        "rid_str": "999888777000111222",
        "jump_url": ""
      },
      "id_str": "999888777000111222",
      "type": "DYNAMIC_TYPE_DRAW",
      "modules": {
        "module_author": {
          "mid": 123456789,
          "name": "某UP主",
          "pub_time": "2025-07-21 23:39:28",
          "pub_ts": 1753164033
        },
        "module_dynamic": {
          "desc": { "text": "动态正文..." }
        },
        "module_stat": {
          "forward": { "count": 45 },
          "comment": { "count": 60 },
          "like": { "count": 1234 }
        }
      }
    }
  }
}
```

### comment_type 枚举值

| 值 | 含义 | 对应 CommentResourceType |
|----|------|--------------------------|
| 1 | 视频 | VIDEO |
| 11 | 图文动态 | DYNAMIC_DRAW |
| 12 | 专栏 | ARTICLE |
| 17 | 纯文字动态 | DYNAMIC |
| 22 | 漫画 | — |
| 29 | 漫画阅读器 | — |

> 实际操作中，动态页的 comment_type 只会出现 1/11/12/17 四种。传评论 API 时直接透传即可。

### 错误处理

- `code != 0`：动态不存在或ID错误
- `data.item` 不存在：动态被删除/隐藏
- `data.item.basic.comment_id_str` 为空：B站数据异常，建议重试

---

## API 2：获取 WBI 密钥

### 为什么需要 WBI 签名

B站从 2023 年开始对部分公开 API（包括评论接口）启用 WBI 签名校验。不上签名直接请求会返回 `code: -403`。

WBI 签名本质是一个**时效性弱的 token**，一套密钥可以用很长时间（数小时甚至数天），不需要每次调评论都重新获取。但为保险起见，建议每次获取评论前重新拉一次，反正就是一个 GET。

### 请求

```
GET https://api.bilibili.com/x/web-interface/nav
```

无需任何参数，无需 Cookie。

### 关键返回字段

```json
{
  "code": 0,
  "data": {
    "wbi_img": {
      "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
      "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"
    }
  }
}
```

两个 URL 看起来是图片，实际上文件名中包含密钥。

### 提取 origin_key

```python
def get_origin_key(img_url: str, sub_url: str) -> str:
    """从两个图片URL提取文件名（去掉扩展名），拼接得到原始key"""
    img_key = img_url.split("/")[-1].split(".")[0]
    sub_key = sub_url.split("/")[-1].split(".")[0]
    return img_key + sub_key
    # → "7cd084941338484aae1ad9425b84077c4932caff0ff746eab6f01bf08b70ac45"
```

### 用混淆表生成 mixinKey

B站用了一个**固定64位混淆映射表**来重排 origin_key，然后取前32位作为最终签名用的 mixinKey。

**这个表是固定的，直接抄就行，不会再变：**

```python
MIXIN_ENCRYPT_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
]

def get_mixin_key(origin_key: str) -> str:
    """按混淆表重排 origin_key，取前32位"""
    return "".join(origin_key[n] for n in MIXIN_ENCRYPT_TABLE)[:32]
```

### 签名计算

签名算法：**参数字典按 key 排序 → URL Query String → 拼上 mixinKey → MD5**

```python
import hashlib
import time
import urllib.parse

def wbi_sign_params(params: dict, mixin_key: str) -> dict:
    """
    对请求参数做 WBI 签名，返回追加了 w_rid 和 wts 的参数字典。

    Args:
        params: 原始请求参数（还没签名）
        mixin_key: 从 WBI 密钥生成的 mixinKey

    Returns:
        追加了 w_rid 和 wts 的参数字典
    """
    # 1. 按 key 字典序排序
    sorted_items = sorted(params.items())

    # 2. 拼成 query string（key=value&key=value）
    query = urllib.parse.urlencode(sorted_items)

    # 3. MD5(query + mixin_key)
    signature = hashlib.md5((query + mixin_key).encode()).hexdigest()

    # 4. 追加到参数
    params["w_rid"] = signature
    params["wts"] = int(time.time())  # 当前 Unix 时间戳（秒）
    return params
```

示例过程：
```python
params = {"oid": 999888777, "type": 11, "mode": 3}
# sorted: mode=3&oid=999888777&type=11
# query: "mode=3&oid=999888777&type=11"
# mixin: "abc123..." (32位)
# md5_input: "mode=3&oid=999888777&type=11abc123..."
# w_rid: "e4d5f6..." (32位hex)
```

---

## API 3：获取主评论列表

### 请求

```
GET https://api.bilibili.com/x/v2/reply/wbi/main
        ?oid={oid}
        &type={comment_type}
        &mode={2|3}
        &pagination_str={offset}
        &w_rid={signature}
        &wts={timestamp}
```

### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `oid` | int | ✅ | API 1 返回的 `comment_id_str`，注意要传数字（不含引号） |
| `type` | int | ✅ | API 1 返回的 `comment_type`，直接透传 |
| `mode` | int | ❌ | `2` = 按时间排序，`3` = 按热度排序。默认 `3` |
| `pagination_str` | str | ❌ | 翻页游标。首次请求不传这个参数；后续从响应的 `cursor.pagination_reply.next_offset` 获取 |
| `w_rid` | str | ✅ | WBI 签名（见上节） |
| `wts` | int | ✅ | 当前 Unix 时间戳（秒），WBI 签名计算时自动追加 |

### Headers

```python
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
    "Referer": "https://www.bilibili.com/",
}
```

B站不会严格校验 User-Agent 和 Referer，但建议带上 Referer 防反爬。

### 翻页机制

该 API 使用**游标（cursor）翻页**，不是传统页码翻页：

1. 首次请求不传 `pagination_str`
2. 响应中 `cursor.pagination_reply.next_offset` 是一个 JSON 字符串，如 `"{\"offset\":\"20\"}"`
3. 下次请求把这个字符串原样传给 `pagination_str` 参数
4. 当 `next_offset` 为 `"no-next-offset"` 时表示没有更多评论

> 注：置顶评论（`top_replies`）只在第一页（不传 pagination_str 时）返回。

### 完整响应结构

```json
{
  "code": 0,
  "message": "0",
  "data": {
    "cursor": {
      "all_count": 60,
      "is_begin": true,
      "prev": 0,
      "next": 1,
      "pagination_reply": {
        "next_offset": "{\"offset\":\"20\"}"
      },
      "mode": 3,
      "name": "",
      "session_id": ""
    },
    "top_replies": [
      {
        "rpid": 111222333444,
        "rpid_str": "111222333444",
        "oid": 999888777,
        "type": 11,
        "mid": 123456789,
        "root": 0,
        "parent": 0,
        "dialog": 0,
        "count": 0,
        "rcount": 0,
        "state": 0,
        "fansgrade": 0,
        "attr": 0,
        "ctime": 1753164033,
        "mid_str": "123456789",
        "oid_str": "999888777",
        "root_str": "0",
        "parent_str": "0",
        "dialog_str": "0",
        "like": 142,
        "action": 0,

        "member": {
          "mid": "123456789",
          "uname": "某UP主",
          "avatar": "https://i0.hdslb.com/bfs/face/placeholder.jpg",
          "rank": "10000",
          "sex": "女",
          "sign": "个人签名示例",
          "level_info": {
            "current_level": 6,
            "current_min": 0,
            "current_exp": 0,
            "next_exp": 0
          },
          "vip": {
            "vipType": 2,
            "vipDueDate": 2000000000000,
            "dueRemark": "",
            "accessStatus": 0,
            "vipStatus": 1,
            "vipStatusWarn": "",
            "theme_type": 0,
            "label": {
              "path": "",
              "text": "",
              "label_theme": "annual_vip",
              "text_color": "#FFFFFF",
              "bg_style": 0,
              "bg_color": "#FB7299",
              "border_color": ""
            },
            "avatar_subscript": 1,
            "nickname_color": "#FB7299",
            "avatar_subscript_url": ""
          },
          "official_verify": {
            "type": 0,
            "desc": "bilibili 知名UP主认证"
          },
          "pendant": {
            "pid": 0,
            "name": "",
            "image": "https://i0.hdslb.com/bfs/garb/placeholder.png",
            "expire": 0,
            "image_enhance": "",
            "image_enhance_frame": ""
          },
          "nameplate": {
            "nid": 0,
            "name": "",
            "image": "",
            "image_small": "",
            "level": "",
            "condition": ""
          },
          "fans_detail": {
            "uid": 123456789,
            "medal_id": 0,
            "medal_name": "粉丝勋章",
            "level": 21,
            "color": 0,
            "special": "",
            "icon_id": 0,
            "score": 0,
            "intimacy": 0,
            "master_status": 0,
            "receive_time": 0,
            "is_receive": true,
            "medal_color_start": 0,
            "medal_color_end": 0,
            "medal_color_border": 0,
            "guard_level": 0,
            "is_lighted": 0,
            "light_status": 0,
            "wearing_status": 0,
            "target_id": 0
          },
          "user_sailing": null
        },

        "content": {
          "message": "A了",
          "emote": {
            "[表情包_示例]": {
              "id": 99999,
              "text": "[表情包_示例]",
              "url": "https://i0.hdslb.com/bfs/emote/placeholder.png",
              "mtime": 1745283840,
              "meta": { "size": 1 },
              "type": 2,
              "attr": 0,
              "gif_url": ""
            }
          },
          "jump_url": {
            "BVxxxxxxxx": {
              "title": "某个视频",
              "pc_url": "https://www.bilibili.com/video/BVxxxxxxxx",
              "prefix_icon": "https://i0.hdslb.com/bfs/emote/xxxx.png",
              "extra": {}
            }
          },
          "at_name_to_mid": {
            "@某用户": 12345
          },
          "vote": null,
          "pictures": [
            {
              "img_src": "https://i0.hdslb.com/bfs/note/xxxx.jpg",
              "img_width": 1080,
              "img_height": 720,
              "img_size": 256.5,
              "img_format": "JPEG"
            }
          ]
        },

        "replies": [
          {
            "rpid": 111222333555,
            "rpid_str": "111222333555",
            "oid": 999888777,
            "type": 11,
            "mid": 123456,
            "root": 111222333444,
            "parent": 111222333444,
            "dialog": 111222333444,
            "count": 0,
            "rcount": 0,
            "state": 0,
            "fansgrade": 0,
            "attr": 0,
            "ctime": 1753164000,
            "mid_str": "123456",
            "oid_str": "999888777",
            "root_str": "111222333444",
            "parent_str": "111222333444",
            "dialog_str": "111222333444",
            "like": 5,
            "action": 0,
            "member": { ... },
            "content": { "message": "支持UP主~", ... },
            "up_action": { "like": false, "reply": false }
          }
        ],

        "up_action": {
          "like": false,
          "reply": false
        },
        "card_label": [
          {
            "rpid": 111222333444,
            "text_content": "UP主觉得很赞",
            "label_color_day": "#FF6699",
            "text_color_day": "#FFFFFF",
            "label_color_night": "",
            "text_color_night": "",
            "type": 0
          }
        ],
        "reply_control": {
          "up_reply": false,
          "sub_reply_entry_text": "相关回复",
          "time_desc": "7-21",
          "location": "IP属地：广东",
          "is_note": false
        },
        "folder": { ... },
        "assist": 0,
        "invisible": false,
        "dynamic_id_str": "999888777000111222"
      }
    ],
    "replies": [ ... ]
  }
}
```

### 一级评论对象字段全局说明

下文默认说"评论对象"，包含置顶评论（`top_replies[0]`）和普通评论（`replies[]`），结构完全一致。

#### 基础标识

| 字段 | 类型 | 说明 |
|------|------|------|
| `rpid` | int | 评论ID（数字），**全局唯一** |
| `rpid_str` | str | 评论ID（字符串），建议用这个避免大数精度问题 |
| `oid` | int | 所属作品ID |
| `type` | int | 评论区类型（同请求参数） |
| `mid` | int | 评论者UID |
| `root` | int | 根评论ID。0 = 自己就是根评论；非0 = 是某根评论下的子评论 |
| `parent` | int | 直接父评论ID。0 = 直接回复主楼层；非0 = 楼中楼回复某人 |
| `dialog` | int | 对话线程ID。同一串楼中楼的 dialog 值相同 |
| `ctime` | int | 评论发布时间，**Unix 秒级时间戳** |

#### 互动数据

| 字段 | 类型 | 说明 |
|------|------|------|
| `like` | int | 点赞数 |
| `action` | int | 当前用户的操作状态（登录态才有意义，未登录恒为 0） |
| `rcount` | int | 子评论总数（准确值） |
| `count` | int | 子评论预览数（最多 3，不准确，以 rcount 为准） |
| `state` | int | 评论状态位掩码（0=正常） |
| `attr` | int | 属性位掩码 |
| `fansgrade` | int | UP主粉丝勋章等级 |

#### `member` — 评论者信息

| 字段 | 类型 | 说明 |
|------|------|------|
| `mid` | str | 评论者UID（字符串） |
| `uname` | str | 昵称 |
| `avatar` | str | 头像URL |
| `sex` | str | 性别（"男"/"女"/"保密"） |
| `sign` | str | 个人签名 |
| `rank` | str | 用户排名 |
| `level_info.current_level` | int | 用户等级 0-6（非大会员最高6，大会员可超过） |
| `vip.vipType` | int | 大会员类型：0=非会员, 1=月度, 2=年度 |
| `vip.vipStatus` | int | 会员状态：0=已过期, 1=正常 |
| `vip.nickname_color` | str | 昵称颜色（会员粉 #FB7299） |
| `official_verify.type` | int | 认证类型：0=个人, 1=机构 |
| `official_verify.desc` | str | 认证描述 |
| `pendant.image` | str | 头像框图片URL |
| `fans_detail.medal_name` | str | 粉丝勋章名称（仅UP主粉丝有此字段，否则为空） |
| `fans_detail.level` | int | 粉丝勋章等级 |
| `user_sailing.cardbg.image` | str | 大航海装饰卡面图（舰队/提督/总督才有） |
| `user_sailing.cardbg.fan.number` | int | 大航海粉丝编号 |
| `user_sailing.cardbg.fan.color` | str | 编号文字颜色 |

#### `content` — 评论内容

| 字段 | 类型 | 说明 |
|------|------|------|
| `message` | str | **评论文本**。emoji 用 `[名字]` 占位符表示，不直接包含图片URL |
| `emote` | object | **emoji 映射表**。key 是文本中的占位符，value 含 `url`（图片URL）和 `meta.size`（1=小/2=大） |
| `jump_url` | object | **超链接映射表**。key 是原始URL/文本，value 含 `title`（显示文本）、`pc_url`（跳转URL）、`prefix_icon`（图标） |
| `at_name_to_mid` | object | **@提及映射表**。key 是 `@昵称`，value 是对应用户的 mid |
| `vote` | object | 投票信息（几乎只出现在视频评论中）。含 `title`、`url` 等 |
| `pictures` | array | **评论图片数组**。每项含 `img_src`（图片URL）、`img_width`、`img_height`、`img_size`（KB） |

##### emoji 还原为图片

```python
def render_message(content: dict) -> str:
    """将评论文本中的 emoji 占位符替换为 img 标签"""
    msg = content["message"]
    for key, info in content.get("emote", {}).items():
        size_class = ["", "small", "large"][info["meta"]["size"]]
        img_tag = f'<img class="emoji-{size_class}" src="{info["url"]}" alt="{key}">'
        msg = msg.replace(key, img_tag)
    return msg
```

##### 图片 URL 去水印

评论图片的 `img_src` 末尾可能带 `@` 参数（如 `@1280w_720h`），去掉即可获得原图：
```python
src.split("@")[0]
```

#### `replies` — 子评论预览

每条子评论结构与一级评论**完全一致**，但多了一个关键字段：

| 字段 | 说明 |
|------|------|
| `root` | 指向**根评论的 rpid**（谁的一楼层下） |
| `parent` | 指向**被回复者的 rpid**（楼中楼里回复了谁） |

子评论也含 `member`、`content` 等完整字段，和一级评论解析方式完全相同。

如果 `rcount > len(replies)` 说明还有更多子评论未返回，需要调子评论 API 拉取。

#### `up_action` — UP主互动

| 字段 | 类型 | 说明 |
|------|------|------|
| `like` | bool | UP主是否点赞了这条评论 |
| `reply` | bool | UP主是否回复了这条评论 |

#### `card_label` — 标签

出现在置顶评论或热评上：

```json
[
  { "text_content": "UP主觉得很赞", "label_color_day": "#FF6699", "text_color_day": "#FFFFFF" },
  { "text_content": "热评", "label_color_day": "#00AEEC", "text_color_day": "#FFFFFF" }
]
```

---

## API 4：获取子评论（楼中楼）

当一级评论的 `rcount > len(replies)` 时，说明有更多子评论需要单独拉取。

### 请求

```
GET https://api.bilibili.com/x/v2/reply/reply?oid={oid}&type={comment_type}&root={root_rpid}&pn={page}&ps=20
```

**不需要 WBI 签名，不需要 Cookie。**

### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `oid` | int | ✅ | 同主评论 |
| `type` | int | ✅ | 同主评论 |
| `root` | int | ✅ | 根评论的 rpid（要展开哪条评论的楼中楼） |
| `pn` | int | ❌ | 页码，从 1 开始，默认 1 |
| `ps` | int | ❌ | 每页条数，默认 20 |

### 响应结构

```json
{
  "code": 0,
  "data": {
    "page": {
      "num": 1,
      "size": 20,
      "count": 30,
      "acount": 0
    },
    "replies": [
      { ... }   // 结构与一级评论完全相同
    ]
  }
}
```

- `page.count`：总子评论数（应等于根评论的 `rcount`）
- `page.num`：当前页码
- 翻页：递增 `pn` 直到 `pn * ps >= page.count`

### 子评论顺序

注意区分 `root` 和 `parent`：
- `root` = 根评论 rpid → 所有同一楼下的子评论 `root` 相同
- `parent` = 直接回复对象的 rpid → 楼中楼里 A 回复 B，A 的 `parent` 就是 B
- 子评论按时间升序排列（早→晚），不是嵌套结构

---

## 翻页逻辑

### 主评论翻页

```python
async def get_all_replies(oid: int, ctype: int) -> list[dict]:
    """翻页获取全部一级评论（不含置顶）"""
    all_replies = []
    pagination_str = None  # 首次不传

    while True:
        params = {"oid": oid, "type": ctype, "mode": 2}
        if pagination_str:
            params["pagination_str"] = pagination_str
        signed = wbi_sign_params(params, mixin_key)

        async with session.get(MAIN_API, params=signed) as r:
            data = (await r.json())["data"]
            all_replies.extend(data.get("replies", []))

            # 取下一页游标
            cursor = data["cursor"]["pagination_reply"]
            next_offset = cursor.get("next_offset", "")
            if not next_offset or next_offset == "no-next-offset":
                break
            pagination_str = next_offset

    return all_replies
```

### 子评论翻页

```python
async def get_all_sub_replies(oid: int, ctype: int, root_rpid: int, total: int) -> list[dict]:
    """翻页获取全部子评论"""
    all_subs = []
    page = 1

    while len(all_subs) < total:
        params = {"oid": oid, "type": ctype, "root": root_rpid, "pn": page, "ps": 20}
        async with session.get(SUB_API, params=params) as r:
            data = await r.json()
            replies = data["data"].get("replies", [])
            all_subs.extend(replies)
            if not replies:
                break
            page += 1

    return all_subs
```

---

## 完整实现示例

```python
"""
B站不登录获取动态置顶评论（含 rpid）
纯 HTTP API 实现，无 Playwright、无 Cookie
"""

import asyncio
import hashlib
import time
import urllib.parse
from typing import Optional

import aiohttp


# ============================================================
# WBI 签名（固定混淆表，直接抄）
# ============================================================

MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def get_mixin_key(origin_key: str) -> str:
    """按混淆表重排 origin_key，取前 32 位"""
    return "".join(origin_key[n] for n in MIXIN_TABLE)[:32]


def wbi_sign_params(params: dict, mixin_key: str) -> dict:
    """对请求参数做 WBI 签名，追加 w_rid + wts"""
    query = urllib.parse.urlencode(sorted(params.items()))
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return {**params, "w_rid": w_rid, "wts": int(time.time())}


# ============================================================
# 核心逻辑
# ============================================================

async def fetch_mixin_key(session: aiohttp.ClientSession) -> str:
    """获取 WBI 密钥并生成 mixinKey"""
    async with session.get("https://api.bilibili.com/x/web-interface/nav") as r:
        wbi = (await r.json())["data"]["wbi_img"]
    # 从两个图片URL中提取文件名拼接
    origin = (wbi["img_url"].split("/")[-1].split(".")[0] +
              wbi["sub_url"].split("/")[-1].split(".")[0])
    return get_mixin_key(origin)


async def get_dynamic_basic(session: aiohttp.ClientSession, dynamic_id: str) -> tuple[int, int]:
    """获取动态的 oid 和 comment_type"""
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
    async with session.get(url, params={"id": dynamic_id}) as r:
        basic = (await r.json())["data"]["item"]["basic"]
    return int(basic["comment_id_str"]), basic["comment_type"]


async def get_top_comment(dynamic_id: str) -> Optional[dict]:
    """
    获取动态的置顶评论（完整对象），无置顶返回 None。
    无需登录、无需 Cookie。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }

    async with aiohttp.ClientSession(headers=headers) as s:
        # ① 获取 oid + comment_type
        oid, ctype = await get_dynamic_basic(s, dynamic_id)

        # ② 获取 WBI mixinKey
        mixin = await fetch_mixin_key(s)

        # ③ 签名并请求评论 API
        params = wbi_sign_params({"oid": oid, "type": ctype, "mode": 3}, mixin)
        async with s.get(
            "https://api.bilibili.com/x/v2/reply/wbi/main", params=params
        ) as r:
            data = await r.json()

        # ④ 返回置顶评论
        top = data["data"].get("top_replies")
        return top[0] if top else None


# ============================================================
# 便捷提取函数
# ============================================================

def extract_rpid(comment: dict) -> str:
    """从评论对象提取 rpid（字符串）"""
    return comment["rpid_str"]


def extract_text(comment: dict) -> str:
    """从评论对象提取纯文本（含 emoji 占位符）"""
    return comment["content"]["message"]


def extract_uname(comment: dict) -> str:
    """从评论对象提取评论者昵称"""
    return comment["member"]["uname"]


async def main():
    rpid = None
    comment = await get_top_comment("999888777000111222")
    if comment:
        rpid = extract_rpid(comment)
        print(f"置顶评论: {extract_uname(comment)}: {extract_text(comment)[:40]}...")
        print(f"rpid: {rpid}")
    else:
        print("无置顶评论")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 总结

| 步骤 | API | 输入 | 输出 |
|------|-----|------|------|
| 1 | `/x/polymer/web-dynamic/v1/detail` | dynamic_id | `oid` + `comment_type` |
| 2 | `/x/web-interface/nav` | — | `mixin_key` |
| 3 | `/x/v2/reply/wbi/main` | oid + type + w_rid + wts | `top_replies[0]`（含 rpid） |
| 4 | `/x/v2/reply/reply` | oid + type + root_rpid | 更多子评论 |

不需要登录，不需要 Cookie，不需要浏览器。整个流程约 3 个 HTTP 请求，耗时 ~1 秒。
