## v1.8.5 (2026-08-24) — 场景一通知附原帖截图

### 新增

- **场景一非 priority 互动通知附原帖截图**：与场景二/三统一，邮件模板原帖上下文卡片
  从"场景二/三专属"放开为全场景通用（截图优先、文本降级）
  - 截图时机放在**入库时**（架构原则：截图入库完成，邮件端零等待）：
    小批量（≤单批上限）立即截，大批量标记待补截，由补截循环分批补齐，
    避免首次大量入库阻塞发现流程
  - priority（日常分享/置顶）不附原帖截图：不入截图流程、不标待补截，
    邮件端因文件不存在自然无原帖卡片
  - 即时通知发送前兜底：非 priority 互动的原帖若尚未有截图文件则现场补截一次
  - 截图器启动条件与补截循环启动条件扩展至场景一（只开场景一时也生效）

---

## v1.8.4 (2026-08-21) — 原帖截图空白/风控页拦截修复

### 修复

- **原帖截图误截空白页或 412 风控页**（`screenshotter.py`，4 层校验拦截，坏图不再进入邮件）：
  1. **HTTP 状态码检查**：goto 后校验响应状态，412/403/429 风控拦截页、代理错误页
     （502/503）直接判失败，不再依赖文字匹配
  2. **风控检测时机后移**：风险文字检测移到 networkidle 之后（页面已稳定），
     拦截页 JS 后渲染也能命中；关键词扩充（访问被拦截/请求被拦截/访问受限/
     Access Denied/Forbidden 等）
  3. **卡片内容校验**：截图前检查卡片无文本且无图片（白屏/登录遮罩/骨架屏）
     判失败，走重试/降级链路
  4. **全页降级前校验**：找不到卡片元素走全页截图时，先确认页面非空白
     （有文本或图片才允许截图）

---

## v1.8.3 (2026-08-21) — 邮件留档自动清理

### 新增

- **留档自动清理**：新增 `screenshot.archive_keep_days` 配置（默认 30 天），
  调度器每日执行一次清理，删除留档目录中超过保留天数的截图 PNG。
  邮件 HTML 发送时已内嵌 base64 图片（PNG 仅发送前读取用），过期删除不影响已留档邮件；
  HTML 留档体积极小永久保留。留档目录磁盘占用进入稳态封顶，不再随运行时间无限增长

---

## v1.8.2 (2026-08-20) — 子评论接口确定性错误码补充

### 修复

- **子评论接口 12022（评论已被删除）重试风暴**：`get_sub_comments` 的确定性错误码名单
  （-400/-404/12002）漏了 12022，评论被删除后每次基线扫查都会触发 50s×3 重试，
  空转 API 调用并刷错误日志。修复：12022 加入确定性错误码名单，快速失败跳过
  （与 -412 风控快速失败同一策略）

---

## v1.8.1 (2026-08-20) — 跨场景监测去重 + 邮件可靠性增强 + 场景二发现逻辑修复

### 背景

- v1.8.0 上线后线上发现三类问题：同一评论区被多场景重复监测、降级路径链接 404、
  话题帖（opus 图文动态）下有目标UP主回复但未收到推送邮件

### 新增

- **跨场景监测去重**：scene2/scene3 发现前按 `comment_oid`（评论区维度）查重，
  已被任意场景占用则跳过（先到先得）；场景一不受限（priority/置顶监测必须保住）。
  互动层 `comment_id` UNIQUE 兜底防重复通知
- **邮件互动内容【UP名】标签**：单封即时邮件/场景二批量/场景三批量/优先级子评论批量/日报
  统一在互动内容前加 `【UP名】` badge（复用现有徽章样式，`up_name` 动态获取，换UP主自动跟随）
- **批量邮件徽章**：主评论/子评论标签（与单封一致）+ footer 追踪补「首条cid」（场景三无单封邮件，靠此定位评论）
- **av 前缀截图支持** + 补截循环挂靠到通用任务区（场景二/三任一启用即启动）

### 修复

- **降级路径 av 前缀 item_id 链接 404**：`build_comment_url/build_item_url` 增加 aid 参数
  （取 `comment_oid` 真实 aid）；视频类判定 = item_type 含 AV 或 item_id 带 av 前缀
- **场景二发现循环漏检永不补检**：话题接口对 opus 条目字段偶发缺失（id_str/type 时好时坏），
  被 `continue` 跳过的条目排在已入库条目之后，而「遇已入库条目 break」导致之后每轮
  第一页第一条即已入库 → 直接停 → 漏检条目永不被补检。
  修复：`break` → `continue`（本页剩余条目检查完，仅停止翻页）
- **子评论接口 -412 风控重试风暴**：`get_sub_comments` 遇到 -412（风控）不再 50s×3 重试
  （风控窗口期内重试必败，纯刷错误日志），改为快速失败返回 banned 标志；
  场景一/三翻页循环识别 banned 后跳过本轮、不打基线、下轮扫查再试（防误报、防漏检）
- **子评论基线扫查限速**：逐行 count 查询增加 0.8s 间隔（失败同样限速），
  降低批量扫查请求密度，减少触发风控的概率

### 已知限制

- 话题活跃时 L1 轮询一轮耗时（7~12 分钟）超过配置间隔，新入库条目互动通知存在同量级延迟
  （防重入锁保证不并发不堆积，属于设计行为）

---

## v1.8.0 (2026-08-19) — 场景三：切片视频评论区监测（目标UP主回复）+ 主评论接口 buvid 污染修复

### 新增：场景三（切片视频评论区监测）

- 新模块 `monitor_scene3.py`：两阶段轮询——阶段A 拉取切片员空间动态入库（source="scene3"），
  阶段B 轮询评论区匹配 `mid == target_uid`
- **配置**（`config.yaml` 新增 `scene3` 段）：
  - `target_uid`：目标UP主UID（在切片评论区中匹配这位的评论/回复）
  - `clip_up_list`：切片账号列表（支持多个，`[{uid, name}]`，name 可留空）
  - `monitor.scene3_enabled`：场景开关
- **切片员昵称自动获取**：main.py 启动时调用 `get_user_info` 自动补全切片员昵称，
  config 只需填 UID（获取失败不阻塞启动）
- **数据归属**：`monitored_items.up_uid` = 切片员UID（作品归属）；
  `interactions.up_uid` = 目标UP主UID（通知/日报按目标UP主聚合，日报自然并入）
- **匹配规则**：评论/子评论 `mid == target_uid`；不查 `up_action.like`（那是切片员的赞，语义同场景二）
- **通知**：全部批量合并（同场景二）——每 `scene2_batch_seconds`（默认180s）合并一封，
  无即时推送；日报照常全量汇总
- **分级**：L1（0~24h）/ L2（24~120h）/ L0（归档），与场景一相同阈值，
  `first_seen_at` 取 `pub_ts` → 首次发现的旧切片自动落 L2/L0
- **子评论**：rcount 基线节流（复用 `sub_comment_baseline` 表 + 2min 最小间隔）+ 尾部窗口翻页
  + 兜底扫查（只处理 scene3 基线）
- **截图**：入库时不截图，直接标记 `screenshot_pending`（首次大量入库暂缓），由补截循环分批补齐；
  补截查询放开 `monitor_level > 0` 限制（历史切片归档后仍补截图）
- **调度**：6 个新循环（发现/L1轮询/L2轮询/重新分级/子评论基线扫查/批量通知），
  scene3 启用且配置了切片员才启动

### 修复：主评论接口被 buvid 污染降级（影响场景一/二/三全部主评论轮询）

**问题**：主会话调过 space feed/topic feed（种下 buvid）后，`x/v2/reply/wbi/main`
可见评论条数被 B站 降级（实测 9 条 → 3 条，且只返回时间最新的子集）——
关键楼（含子评论的根评论）会从可见列表消失 → 子评论基线建不起来 → 目标回复漏检。
与子评论翻页 buvid 截断是**同一机理**，只是降级表现不同。

**修复**：`get_comments` 改走独立干净会话（无 buvid）+ 手动 WBI 签名，与子评论接口同一方案。
A/B 实测确定性复现：主会话 space feed 后 3 条（关键楼消失），干净会话恒 9 条（含关键楼）。

### 改进：邮件渲染清洗 B站 自动搜索词链接

- B站 会为评论中的关键词自动生成"评论内容自动搜索"链接（`content.jump_url`），
  渲染后正文里的词语会变成指向 B站 搜索页的超链接，对邮件读者是噪音。
  `render_comment_html` 不再渲染 jump_url 链接（评论原文保留纯文本）。

### Claude (AI Assistant) 的贡献

- **代码实现**：monitor_scene3.py 全模块、scene3 配置解析、6 个调度循环、
  批量邮件发送、截图分批补齐适配、切片员昵称自动获取
- **根因分析**：实测复现主评论接口 buvid 污染降级（主会话 vs 干净会话 A/B 对照），
  修复 get_comments 走干净会话
- **邮件清洗**：定位"自动搜索"链接来源（jump_url 渲染），移除后正文纯净
- **验证**：真实数据端到端验证（发现→轮询→子评论基线→命中目标UP主回复→批量邮件）、
  服务器部署后日志/DB 双确认

---

# Changelog

EchoWatch（留声）版本更新记录。

---

### 补充修复（2026-08-19，未打版本 tag）：场景三 sweep 子评论基线扫查 root_context None 防御

**问题**：场景三 `sweep_sub_comment_baselines` 兜底扫查调用 `_process_sub_comments` 时
`root_context` 传 None（无主列表上下文），但当目标UP主直接回复楼中楼根评论
（`parent_rpid == root_rpid`）时，代码直接 `root_context["content"]` 取值 → 崩溃
`'NoneType' object is not subscriptable` → 整轮 sweep 中断，且基线已更新不重翻，
存在互动永久漏检风险。场景一 v1.6.0 已修过同样问题，场景三 v1.8.0 新增时未带上该防御。

**修复**：与场景一同一方案，`root_context` 为 None 时 parent 字段置空
（`parent_content = root_context["content"] if root_context else ""`），sweep 路径不再崩溃。

#### Claude (AI Assistant) 的贡献

- **缺陷修复**：场景三 sweep 路径 root_context None 防御（对齐场景一 v1.6.0 方案）
- **验证**：服务器日志复现确认（sweep 异常栈）、数据库核对无漏检、修复后重启日志确认无新异常

---

### 补充修复（2026-08-18，未打版本 tag）：邮件 HTML 注入与路径穿越安全加固 + 6 项修复

**问题**：邮件模板中 UP主昵称、父评论作者、发现时间等外部可控字段直接拼入 HTML，
恶意内容可注入 script 标签或 javascript: 协议链接；动态 ID 未经校验直接拼入截图/归档文件名，
存在路径穿越写入风险；另有退出清理顺序、buvid 失败重试、配置写入原子性等隐患。

**修复**：
- **邮件 HTML 注入**：新增 `_safe_url()`（仅放行 http/https，拦截 javascript:/data:/vbscript:）与
  `_safe_filename()`（文件名白名单清洗）；外部可控字段（parent_author、up_name、up_uid、时间、
  标题/描述、追踪信息等）全部 `html.escape`，img src / a href 属性位全量转义
- **路径穿越**：`take_dynamic_screenshot()` 入口校验 dynamic_id 必须为纯数字（`^\d+$`），
  非法直接返回 None；邮件归档文件名统一走 `_safe_filename()` 清洗
- **退出清理**：Scheduler 新增 `stop()`（取消全部调度任务并等待退出）；main 的 finally 顺序修正为
  scheduler.stop() → client.close()（补齐漏关的 BiliClient）→ db.close() → screenshotter.stop()
- **config.py 测试入口**：删除不存在的 `up.priority_mode` 引用，消除 AttributeError
- **预测窗口死代码**：`_check_prediction_clear()` 改为按 `topic_offset.updated_at` 判断空闲时长，
  删除「当天无互动就永远不清空」的无效分支
- **buvid 失败节流**：获取失败不再永久标记 ready，改为 60s 节流重试，避免每次 API 调用都打 B站首页
- **配置原子写入**：config.yaml 改为先写 `.tmp` 再 `os.replace()` 原子替换，失败时清理临时文件

#### Claude (AI Assistant) 的贡献

- **安全加固**：邮件模板注入面梳理与转义、URL 协议白名单、文件名清洗、截图动态 ID 校验
- **缺陷修复**：退出清理顺序、buvid 失败节流、配置原子写入、预测窗口死代码移除
- **验证**：逐项行为测试（注入用例、路径穿越用例、原子写入实测、语法编译检查）

---

## v1.7.0 (2026-08-15) — 日志轮转系统：logs 独立目录 + error.log 分离 + 3 天自动清理

### 背景

- 原日志直接写在项目根目录，主日志与轮转备份散落根目录，与代码文件混在一起
- 错误与常规日志混在同一文件，排查异常时需要全量检索
- 参照 BTCE 项目的日志模式统一改造

### 改动

| 改动 | 文件 |
|------|------|
| 日志统一迁移到 `logs/` 独立目录（路径由 `logging.file` 配置，默认 `logs/echowatch.log`） | `logger_config.py` `config.py` |
| 新增 `logs/error.log`：仅记录 ERROR 及以上，排查异常直接看此文件 | `logger_config.py` |
| 启动时自动清理超过 3 天的旧日志文件（含轮转备份），与 BTCE 保留策略一致 | `logger_config.py` |
| 大小轮转参数不变：单文件 5MB × 3 备份，主日志总量上限约 20MB | `logger_config.py` |
| 配置模板默认路径同步 | `config.example.yaml` |

### 部署

- 代码文件 2 个：`logger_config.py` `config.py`（服务器 config.yaml 仅同步 `logging.file` 一行）

### Claude (AI Assistant) 的贡献

- **代码实现**：logs/ 独立目录、error.log 独立错误日志、3 天旧日志自动清理（与 BTCE 日志系统对齐）
- **文档**：README 版本演进表与项目结构同步更新

---

### 补充修复（2026-08-15 同日，未打版本 tag）：-352 风控快速失败 + 缓存兜底

**问题**：动态列表接口 `x/space/wbi/arc/search` 受 -352 风控校验失败打击（与 -412 并存），
旧逻辑失败后 50s×2 快速重试——刚被风控就连续重打正是 bot 特征，会延长风控，
且风控期间动态列表获取完全中断。

**修复**：
- `-352`/`-412` 识别为风控类错误，放弃本轮不重试（等下一轮发现自然恢复）
- 动态列表成功结果缓存（1 小时有效），风控期间返回缓存兜底，监测不断流
- 网络类瞬态错误仍走原有 async_retry 重试机制

#### Claude (AI Assistant) 的贡献

- **根因分析**：结合项目历史（v0.2 未签名接口曾报 -352）与社区反馈（该接口风控严重），
  判定 -352 为请求频率/特征风控而非 WBI 签名错误（签名错会 100% 失败，实测是间歇性）
- **代码实现**：风控错误识别、动态列表缓存兜底、快速失败逻辑

---

## v1.6.0 (2026-08-14) — 子评论翻页 buvid 截断终局修复 + 尾部窗口翻页 + Priority 子评论批量汇总

### 问题（漏检复盘）

2026-08-09 ~ 08-14 线上子评论检测全面失效：置顶动态下UP主的多条楼中楼回复与觉得很赞全部漏检漏发（排查期间 interactions 表 is_sub_reply=1 记录为零）。
排查确认**终局根因**：

1. **buvid cookie 静默截断子评论翻页（核心根因）**：会话种下真实 buvid3+b_nut 后，
   `x/v2/reply/reply` 从第 2 页起静默返回空列表（code=0 不报错）→ 翻页永远只拿到第 1 页
   10 条 → UP 回复（楼中楼按时间升序，UP 回复靠后）全部漏检。
   服务器 A/B 实测确定性复现：干净会话翻页 100% 正常；种 buvid 后必截断。
   （Bilibili-Freeview 插件用 `credentials:'omit'` 不带 cookie 所以免疫）
2. **基线照常打戳放大漏检**：翻页被截断仍按完整 count 更新基线 → 节流逻辑误判"已检查"→
   永久漏检（收集不全却误标基线）
3. **12002 重试风暴**：评论区已关闭的 item 每次 50s×3 重试，拖垮 sweep 循环
4. **IP 风控**：密集排查请求把机房代理出口 IP 打爆（reply 接口 -412 HTML 风控页），
   通过 mihomo API 切换订阅节点恢复（香港-02 → 新加坡-01）

### 修复

| 修复 | 文件 |
|------|------|
| 子评论专用独立干净会话 `_get_sub_session()`（独立 CookieJar，永不种 buvid）+ 请求内联解包 `data.data` | `bili_client.py` |
| `get_sub_comments` 确定性错误（-400/-404/12002）快速失败不重试 | `bili_client.py` |
| 尾部窗口翻页：日常触发只翻 `[基线-2页, 末页]`；首次/sweep 强制才全量扫（上限 100 页）；**窗口没翻完不打基线**（防误标，下一轮重翻） | `monitor_scene1.py` |
| sweep 传 `force_full=True` 全量扫查兜底 | `monitor_scene1.py` |

### 新增：Priority 通知分流

- **主评论（含根评论的赞）→ 即时邮件**（保持原样）
- **子评论（含楼中楼的赞）→ 每 3 分钟合并一封**（`_priority_sub_batch_loop`，
  `send_priority_sub_batch` 复用场景二汇总模板；间隔 `priority_sub_batch_seconds` 可调）
- **日报 22:00 照常汇总全部**（批量只标记 notified_immediate，不动 notified_digest）

### 新增：被回复内容表情渲染

- interactions 表新增 `parent_rich_content` 列（数据库迁移自动加列）
- scene1/scene2 的 root_context 与子评论查找表均携带 rich_content
- 单封邮件与汇总模板对被回复内容统一走 `render_comment_html`（表情/图片可见）


### 部署

- 代码文件 7 个：`bili_client.py` `config.py` `database.py` `email_notifier.py`
  `monitor_scene1.py` `monitor_scene2.py` `scheduler.py`（不覆盖 config.yaml）
- 重启后必须确认代理环境变量 `HTTPS_PROXY=127.0.0.1:7890` 在位（`pm2 restart --update-env`）

### Claude (AI Assistant) 的贡献
- **根因定位**：服务器 A/B 实验（干净会话/种 buvid/去 cookie 对照）确定性复现 buvid 截断；
  交叉验证参考项目的实现差异
- **代码实现**：独立干净会话、12002 快速失败、尾部窗口翻页（含窗口未翻完不打基线）、
  Priority 通知分流（批量循环 + 汇总模板复用）、被回复内容富内容渲染（迁移+双场景+双模板）
- **运维**：mihomo 节点切换（IP 风控）、部署与日志验证

---

## v1.5.0 (2026-08-11) — 评论翻页 -400 真因修复 + 子评论基线兜底扫查

### 问题（漏检复盘）
置顶动态一条根评论（某根评论）的 UP 子回复（08-10 11:11 发布）漏检，排查发现
主评论翻页第 2 页起必 -400 的**真根因**（v1.4.0 误判为"实际仅一页的评论区传游标才 -400"）：

1. **pagination_str 格式错误（核心根因）**：B站 要求游标以 `{"offset": <裸值>}` JSON 格式传递，
   直接传裸字符串（如 `CAEaADIDCMoE`）第 2 页起必 -400 → 主评论永远只翻到第 1 页（20 条）→
   根评论滑出首页后 check3（子评论检测）永不触发 → 静默漏检。
   实测：裸值 → -400（0 条）；`{"offset":...}` 包装 → 正常返回 20 条
   （参考 Bilibili-Freeview 插件实现，B站前端一直用 JSON 包装）
2. **活进程 API 响应截断（观察现象，根因未完全定位）**：持续高频轮询下活进程 get_comments
   只返回 3 条最新+置顶，独立进程同参数调用返回 19~20 条 —— 根评论不在返回列表 → check3 永不触发。
   已用兜底扫查缓解（见下），后续若复现再深挖

### 修复（4 项）
| 修复 | 文件 |
|------|------|
| `pagination_str` 内部 JSON 包装 `{"offset": 裸值}`（调用方仍传裸值） | `bili_client.py` |
| 子评论每页 `ps` 20→10（与 B站 前端一致） | `bili_client.py` |
| 子评论翻页终止条件改「不满一页即末页」（原 `page*20>=total` 在 ps=10 下提前 4 页 break 漏数据）+ 翻页上限 10→15 页 | `monitor_scene1.py` / `monitor_scene2.py` |
| 新增子评论基线兜底扫查（sweep）：不依赖主列表可见性 | `monitor_scene1.py` / `scheduler.py` / `database.py` / `config.py` |

### 新增：子评论基线兜底扫查（sweep）
- 每 `sub_sweep_interval`（默认 10 分钟）遍历 `sub_comment_baseline` 全表
- 对每条根评论直接用子评论 API 查权威 count（每条 1 次请求，节流）：
  - count > 基线 且 距上次翻 ≥ 2min → 完整翻页检测
  - 基线超过 `sub_sweep_max_age`（默认 30 分钟）→ 强制重新翻页
    （防「收集不全却误标基线」导致的永久漏检——实测发生过：截断环境下基线被标到 109 但实际只收集 60 条）
- 无主列表上下文时 root_context 传 None，parent 显示留空

### up_action.like 语义修正（重要）
- `up_action.like` 语义 = **评论区作者**（评论 oid 对应内容的发布者）的点赞
- 场景2 话题监测包含粉丝发布的帖子 → 粉丝帖的评论区作者是粉丝 → up_action.like 是
  **发帖粉丝**的赞，不是UP主（2026-08-11 实测：scene2 item 作者 mid 大量非 UP主UID）
- **场景2 删除「觉得很赞」检测**：sweep 对 scene2 item 传 `check_up_liked=False`；
  scene2 常规轮询原本就不检测（仅 UP 回复）
- 场景1（UP主自己的作品）保留「觉得很赞」检测：评论区作者即UP主，语义正确
- 实测子评论级 up_action.like 是独立准确值（非楼层传播）：根评论未赞（like=false）的楼，
  子评论可单独 like=true

### 部署
- 代码文件 6 个：`bili_client.py` `config.py` `database.py` `monitor_scene1.py` `monitor_scene2.py` `scheduler.py`（不覆盖 config.yaml）
- 重启后必须确认代理环境变量 `HTTPS_PROXY=127.0.0.1:7890` 在位（`pm2 restart --update-env`）

### Claude (AI Assistant) 的贡献
- **根因定位**：实测对比裸值/JSON 包装两种游标格式，确认 -400 真因（修正 v1.4.0 的错误归因）；
  排查并记录活进程响应截断现象
- **代码实现**：pagination_str JSON 包装、ps=10、翻页终止条件修复、子评论基线兜底扫查（sweep）
- **连带修复**：ps 改 10 后 `page*20` 终止条件提前 break 漏数据的 bug

---

## v1.4.0 (2026-08-09) — 评论翻页 -400 重试风暴修复 + 子评论检测基线节流

### 问题（线上事故复盘）
置顶动态一条根评论（某根评论）的 UP 子回复（08-10 11:11 发布）漏检，排查发现
主评论翻页第 2 页起必 -400 的**真根因**（v1.4.0 误判为"实际仅一页的评论区传游标才 -400"）：

1. **主评论翻页 -400 触发 150s 重试风暴**：B 站 `x/v2/reply/wbi/main` 对部分评论区（实际仅一页）的翻页游标 `pagination_str` 返回 `-400 请求错误`。`get_comments` 带 `async_retry(API_RETRY)`（50s×3），每次翻页失败卡 150 秒
2. **`poll_priority_only` 无防重入锁**：priority 轮询每 1~5s 启动一轮，上一轮卡在 150s 重试期间新轮继续堆积 → 并发请求风暴 → 触发 B 站风控（-412），风暴期间子评论检查无法完成
3. **子评论翻页上限 5 页（100 条）**：107 条子评论的"大楼"（111222333444）尾部 7 条（含 UP 回复）永远取不到，6 小时后才补检

### 修复（4 项）
| 修复 | 文件 |
|------|------|
| 确定性错误（-400/-404/12002）快速失败不重试，返回 `disabled` 标记 | `bili_client.py` |
| `poll_priority_only` 加防重入锁（与 `poll_all` 一致） | `monitor_scene1.py` |
| 子评论翻页上限 5→10 页（200 条） | `monitor_scene1.py` / `monitor_scene2.py` |
| 场景二评论已关闭（12002）的帖子跳过轮询 1 小时 | `monitor_scene2.py` |

### 新增：子评论检测基线节流（v1.4.0 核心）
- 新表 `sub_comment_baseline(item_id, root_rpid, last_rcount, last_check_ts)`，主键 `(item_id, root_rpid)`，**只存当前值**（翻页后 upsert 覆盖，不保留历史）
- 触发规则：基线无记录（首次见根评论）→ 立即翻 + 写基线；有记录 → **rcount 变大 且 距上次翻 ≥ 2min** 才翻
- 翻页成功才更新基线（失败等下一轮重试，不吞新回复）
- 效果：priority 每轮请求量从 ~15 次降到 1 次主评论请求；场景二（95+ 帖子）不再每轮全翻子评论，负载降幅最大

### 验证
- 本地单测 16 项全通过（确定性错误不重试 / 防重入锁 / 翻页上限 / disabled 跳过 / 基线节流 8 用例）
- 服务器实测：重启后零条 50s 重试日志；基线 2 分钟内 `last_check_ts` 不变（节流生效）；priority 轮询 last_polled_at 持续更新

### 文件清单
| 文件 | 变化 |
|------|------|
| `bili_client.py` | 修改 — 确定性错误快速失败 + disabled 标记 |
| `database.py` | 修改 — 新增 sub_comment_baseline 表 + get/upsert 方法 |
| `monitor_scene1.py` | 修改 — priority 锁 + 子评论 10 页 + 基线节流 |
| `monitor_scene2.py` | 修改 — disabled 跳过 1 小时 + 子评论 10 页 + 基线节流 |

### XTong 的贡献
- **事故定位**：提供具体根评论链接追踪漏检邮件，确认 2min 子评论延迟可接受
- **方案设计**：基线入库（只更新值不记历史）+ rcount 变大与 2min 间隔双重触发条件

### Claude (AI Assistant) 的贡献
- **根因排查**：服务器实测定位三级根因链（-400 重试风暴 → 无锁并发堆积 → 翻页上限截断）
- **实现**：4 项修复 + 基线节流落地，本地单测与服务器部署验证

---

### 补丁 (2026-08-07) — 汇总邮件评论直达按钮移至条目头部行

### 问题
- 汇总邮件中「评论直达」按钮独占一行，位于评论内容下方，头部时间/场景/类型行显得松散，条目不够紧凑

### 修复
- `email_notifier.py`：`build_digest_email` 条目头部行改为 flex 布局——左侧 `#N 时间 场景 类型`，右侧内联缩小版「评论直达」按钮（padding 3px 12px / 字号 11px），删除原独立按钮行；保留渐变底色样式

### 文件清单
| 文件 | 变化 |
|------|------|
| `email_notifier.py` | 修改 — 按钮移入头部行 + flex 布局 |

### XTong 的贡献
- **方案提出**：评论直达按钮应和条目头部信息同一行

### Claude (AI Assistant) 的贡献
- **实现**：flex 头部行 + 内联小按钮落地，语法编译验证通过

---

### 补丁 (2026-08-05) — 截图失败自动补截：标记 + 重试循环 + 发送前兜底

### 问题
- 新入库时截图失败（风控/登录遮罩/浏览器异常）后没有任何记录，该动态永远没有截图，邮件只能长期文本降级
- 截图失败是间歇性的（风控几秒恢复），错过一次就没有第二次机会

### 修复
- `database.py`：`monitored_items` 新增 `screenshot_pending` 列（老库自动迁移），新增 `mark_screenshot_pending` / `clear_screenshot_pending` / `get_screenshot_pending_items` 三个方法
- `monitor_scene2.py`：入库截图失败（返回 None 或抛异常）→ 标记 `screenshot_pending=1`；新增 `retry_screenshots()` 批量补截（单批限流 `max_per_batch` 张，成功清标记，失败保留等下轮）
- `scheduler.py`：新增截图补截循环（每 `screenshot.retry_interval` 秒，默认 600s）；批量通知发送前兜底——待发互动对应原帖若无截图文件则现场补截一次（发送时离入库已过一段时间，补截成功率更高；失败不影响发送，自动降级文本）
- `config.py` / `config.yaml`：新增 `screenshot.retry_interval` 配置项
- `main.py`：scheduler 注入 screenshotter 引用

### 文件清单
| 文件 | 变化 |
|------|------|
| `database.py` | 修改 — screenshot_pending 列 + 3 个方法 |
| `monitor_scene2.py` | 修改 — 失败标记 + retry_screenshots() |
| `scheduler.py` | 修改 — 补截循环 + 发送前兜底 |
| `config.py` / `config.yaml` / `config.example.yaml` | 修改 — retry_interval 配置 |
| `main.py` | 修改 — scheduler 注入截图器 |

### XTong 的贡献
- **方案提出**：新入库截图失败应标记动态并等待下次补截

### Claude (AI Assistant) 的贡献
- **实现**：标记/补截/发送前兜底三层机制落地，本地模拟验证全流程（失败保留标记、重试成功清标记）

---

### 补丁 (2026-08-05) — 场景2截图修复：登录遮罩/412风控页不再入邮件

### 问题
- 场景2动态截图截到 B站 强制登录遮罩页：headless 无痕浏览器无 buvid cookie 直访 `t.bilibili.com/{id}` 被 B站弹登录框（TITLE=「动态-哔哩哔哩」，正文全是「登录后你可以/立即登录」，动态内容不渲染）——本机家宽 IP 直连也触发，与 IP 无关，纯无 cookie 触发
- 页面无卡片时现有降级「全页截图」把登录页/风控页整个截下来，邮件端只看 PNG 文件存在与否就嵌入 → 垃圾截图发进邮件
- 服务器场景还会间歇性命中 412 风控页（机房 IP 无代理直连）

### 修复
- `screenshotter.py` 新增 `_seed_cookies()`：截图前先在**同一 context** 访问 B站首页种下 buvid 等游客 cookie，再访问目标动态页（实测种 cookie 后 TITLE=「UP主名动态」，卡片正常渲染）
- 新增 412 风控检测：页面文本含 `request was banned/访问异常/访问过于频繁` 或 URL 丢失动态 ID → 判失败抛异常，走重试/降级，绝不把风控页当动态卡片截下
- 截图 context 显式走代理：从环境变量读 `HTTPS_PROXY`（服务器 PM2 已配 7890），与 API 层同一出口绕开机房 IP 412
- 重试间隔 2s → 5s：间歇性 412 几秒恢复，等足时间再重试

### 文件清单
| 文件 | 变化 |
|------|------|
| `screenshotter.py` | 修改 — 种 cookie + 412 检测 + 显式代理 + 重试间隔 |

### XTong 的贡献
- **问题发现**：指出场景2动态截图会截到 412 页面

### Claude (AI Assistant) 的贡献
- **排查 + 修复**：本地实测定位真实根因（无 cookie 登录遮罩而非 412），落地种 cookie + 检测兜底组合方案

---

### 补丁 (2026-08-04) — 置顶变更只允许"新替旧"：杜绝空白替换误发取消邮件

### 问题
- 云服务器 19:39 误发「置顶动态已取消」邮件：space feed API 正常返回但**漏检置顶标签**（无 -412 风控报错，纯间歇性漏检）时，系统把"未检测到置顶"当成"置顶被取消"，触发：置空状态文件 + 清空 config 的 `priority_dynamics` + `clear_stale_priority([])` 清掉全部 DB 标记 + 发取消邮件
- 一小时后（20:39）同一置顶又被检出，系统再次误判"首次发现"重复发邮件 —— 置顶从头到尾没变过

### 修复
- `pinned_dynamic_monitor.check_pinned_dynamic`：API 返回 None（未检测到置顶）时**不再视为变更**——保留状态文件、不清理、`current_id` 保持旧置顶，仅记 warning 日志。只有检测到**新的置顶ID（非空）**才允许替换旧的
- `scheduler._pinned_dynamic_check_loop`：删除"置顶消失清除标记"分支（`changed=True` 时 `current_id` 必为新置顶ID，不再出现 None 替换）
- `monitor_scene1._sync_priority_dynamics`：自动发现为空且 config 未配置时**不再清空** DB 旧 priority 标记，保留现状等待新置顶（防 discover 每轮误清）
- 真实取消置顶（不设新置顶）不再单独通知 —— 权衡：feed 置顶标签检测不可靠，无法区分"真取消"与"漏检"，按"新替旧"原则不通知

### 文件清单
| 文件 | 变化 |
|------|------|
| `pinned_dynamic_monitor.py` | 修改 — None 不再视为置顶消失/变更 |
| `scheduler.py` | 修改 — 删除置顶消失清除分支 |
| `monitor_scene1.py` | 修改 — 无置顶ID时保留旧 priority 标记 |

### XTong 的贡献
- **问题发现**：收到"置顶取消"邮件，指出正确逻辑应为"新替旧 + L1/L2 重新分级"，不允许空白替换

### Claude (AI Assistant) 的贡献
- **排查 + 修复**：服务器日志确认 19:39 漏检→取消邮件、20:39 复检→首次发现邮件的时间线，落地"新替旧"原则

---

### 补丁 (2026-08-04) — 邮件互动统计语义修正：本轮 vs 今日累计

### 问题
- 汇总邮件 stats 区「今日互动：N 条」实际用的是本轮发送列表的长度，并非今日全天累计 —— 每轮批量邮件都会把「今日」显示成本轮条数，误导统计

### 修复
- `build_digest_email` 新增 `today_count` 参数，stats 区分显示：
  - 「本轮互动：N 条」— 本次发送的条数
  - 「今日累计：M 条」— 数据库当天入库的全部互动数（含之前几轮已发送的），按 UP 过滤
- 场景二批量邮件：按本批涉及的 UP 分别查 `get_today_interactions(uid)` 求和
- 日报：`send_daily_digest` 补 `up_uid` 参数（scheduler 传入），按 UP 查全天总量；顺带修复原日报未按 UP 过滤查询的隐患（多 UP 时每次日报查全部未入日报互动）

### 文件清单
| 文件 | 变化 |
|------|------|
| `email_notifier.py` | 修改 — 汇总邮件 stats 本轮+今日累计双显示，批量/日报查询今日累计 |
| `scheduler.py` | 修改 — 日报调用传入 `up.uid` |

### XTong 的贡献
- **问题发现**：邮件「今日互动」条数与本轮不一致，应为累加的今日总量

### Claude (AI Assistant) 的贡献
- **排查 + 实现 + 部署**：定位统计语义问题，接入 `get_today_interactions` 按 UP 累加，已部署服务器并确认服务正常

---

### v1.3.1 (2026-07-31)

### 核心变化
- **场景二截图精确到卡片元素**：放弃 `full_page` 全页截图，改用 Playwright 元素定位截取 `.bili-dyn-item` 动态卡片（照搬 BTCE3.0 方案）
  - 视口从 1080×1920 调整为 700×1200，匹配卡片实际宽度，减少无用区域
  - 找不到卡片元素时降级为全页截图，兼容 B站 DOM 变更
- **邮件截图适当缩放**：新增 `.screenshot img` CSS 约束 `max-width:560px;width:100%`（适配 600px 邮件容器），2x Retina 截图自动等比缩放
  - `_embed_screenshot()` 返回值改为 `<div class="screenshot"><img ...></div>` 包裹结构，居中 + 阴影

### 文件清单
| 文件 | 变化 |
|------|------|
| `screenshotter.py` | 修改 — 元素级截图替代全页截图，视口缩至 700×1200 |
| `email_notifier.py` | 修改 — 新增 `.screenshot img` CSS + `_embed_screenshot` 包裹容器 |

### XTong 的贡献
- **需求**：截图范围精准化（动态卡片 vs 整个网页）+ 邮件中图片适当缩放

### Claude (AI Assistant) 的贡献
- **实现**：参照 BTCE3.0 `monitor.py` 卡片截图逻辑，定位元素截图 + CSS 缩放方案

---

### 补丁 (2026-08-01) — 场景一 priority 项重复发邮件修复

### 问题
- 场景一 L1 轮询中，priority 项被 `poll_priority_only`（高频1~5s）和 `poll_all`（常规300s）两条路径并发轮询
- 新互动被 `_poll_item` 内联发送和 `_immediate_notify_loop` 30秒扫描同时捕获 → 同一互动发两封邮件
- priority 更换后旧项 `is_priority` 被清除但 `monitor_level` 仍为 L1，未立即重新分级
- `clear_stale_priority([])` 空参数时直接 return，置顶消失场景清不掉旧标记

### 修复
- `_poll_all_impl` 排除 `is_priority=True` 项，不再与 `poll_priority_only` 重复轮询
- `_immediate_notify_loop` 跳过 priority 项的互动（已由 `_poll_item` 内联覆盖）
- `_sync_priority_dynamics` 清除旧 priority 后立即调用 `recheck_all_levels` 重新分级
- `clear_stale_priority` 支持空 `keep_ids`（清除全部 priority 标记）
- `get_unnotified_immediate` 调用加上 `scene="scene1"` 过滤

### XTong 的贡献
- **问题发现**：通过云服务器邮件推送定位到同一 comment_id 1秒内双邮件

### Claude (AI Assistant) 的贡献
- **排查 + 实现**：追踪并发竞争路径，修复 3 个文件 7 处改动

---

### 补丁 (2026-08-04) — 置顶自动更换失效：服务器IP被space feed风控（-412）

### 问题
- 服务器（腾讯云机房IP）直连 B站 space feed API 被间歇性风控（`-412 request was banned`，实测约20%概率、几秒恢复）
- `get_pinned_dynamic_id()` 内部 `try/except` 吞掉 `RuntimeError` 返回 `None`，导致 `@async_retry` 装饰器完全不生效 —— 1小时一次的检测碰上一次风控即漏检，置顶自动更换长期失效
- API 失败返回 `None` 会被 `check_pinned_dynamic()` 误判为「置顶已消失」，存在误清状态文件的风险
- PM2 进程未配置代理环境变量，`trust_env=True` 形同虚设

### 修复
- `bili_client.py`：`get_pinned_dynamic_id()` 内部轻量重试（4s × 3 次），重试耗尽抛 `RuntimeError`；返回 `None` 仅表示 API 正常但确实无置顶 —— 严格区分「API 失败」与「无置顶」
- `pinned_dynamic_monitor.py`：`check_pinned_dynamic()` 捕获 API 异常后跳过本轮，不再把风控误判为置顶消失
- `screenshotter.py`：截图增加重试兜底（最多 3 次，失败自动重建浏览器，防止浏览器崩溃后所有截图全挂）；logger 从标准库 logger 切换为项目 `logger_config`（此前截图成功/失败日志完全不可见）
- **部署**：PM2 进程配置 `HTTPS_PROXY=http://127.0.0.1:7890`（mihomo），规避机房 IP 风控；手动同步新置顶入库（状态文件 + config.yaml + DB 三处一致）

### 文件清单
| 文件 | 变化 |
|------|------|
| `bili_client.py` | 修改 — 置顶检测加重试，区分 API 失败与无置顶 |
| `pinned_dynamic_monitor.py` | 修改 — API 失败跳过本轮，防误判置顶消失 |
| `screenshotter.py` | 修改 — 截图重试兜底 + 浏览器崩溃重建 + logger 接入项目日志 |

### XTong 的贡献
- **问题发现**：发现UP主更换置顶后轮询频率仍是 L1，Priority 未自动切换

### Claude (AI Assistant) 的贡献
- **排查 + 修复 + 部署**：定位 -412 风控根因与重试失效链路，代理配置 + 手动数据同步 + 代码加固

---

### v1.3.0 (2026-07-30)

### 核心变化
- **置顶动态自动发现**：不再需要手动填写 Priority 动态 ID，系统自动从 B站 space feed API 发现置顶动态（检查 `module_tag.text == "置顶"`）
  - 新增 `pinned_dynamic_monitor.py`：自动检测模块，参照 [BTCE3.0](https://github.com/X-tong2568/BTCE) 同功能设计，但改用 EchoWatch 的零Cookie + WBI 签名方案
  - `bili_client.py` 新增 `get_pinned_dynamic_id()` 方法
  - 状态持久化到 `pinned_dynamic_state.json`（运行时）+ `config.yaml`（重启兜底）
- **置顶变更自动跟踪**：scheduler 每小时检测一次置顶是否更换
  - 发现新置顶 → 自动入库 `monitored_items`（`is_priority=True`）
  - 置顶更换 → 旧标记清除 + 新置顶入库 + 邮件通知
  - 置顶消失 → 清除标记 + 邮件通知
- **config.yaml 自动回写**：置顶变更时自动更新 `config.yaml` 中对应 UP主 的 `priority_dynamics`，重启后兜底可用
- **置顶变更邮件通知**：`email_notifier.py` 新增 `send_pinned_change()`，渐变卡片风格，包含新旧置顶 ID 直达链接

### 文件清单
| 文件 | 变化 |
|------|------|
| `pinned_dynamic_monitor.py` | **新增** — 置顶动态自动发现+变更检测+状态管理+config同步 |
| `bili_client.py` | 修改 — 新增 `get_pinned_dynamic_id()` |
| `config.py` | 修改 — `IntervalsConfig` 新增 `pinned_check_interval` |
| `config.yaml` | 修改 — 新增 `pinned_check_interval` 配置项 |
| `config.example.yaml` | 修改 — 同步更新 |
| `monitor_scene1.py` | 修改 — `_sync_priority_dynamics()` 自动发现优先+config兜底 |
| `scheduler.py` | 修改 — 新增 `_pinned_dynamic_check_loop()` + 置顶变更处理 |
| `email_notifier.py` | 修改 — 新增 `send_pinned_change()` |
| `.gitignore` | 修改 — 新增 `pinned_dynamic_state.json` |

### XTong 的贡献
- **需求**：当前 Priority 地址手动指定，需参照 BTCE 加上自动监测与变更通知
- **架构决策**：全自动模式（不区分手动/自动），API 自动发现置顶 + config 兜底

### Claude (AI Assistant) 的贡献
- **实现**：`pinned_dynamic_monitor.py` 核心模块、`bili_client.get_pinned_dynamic_id()`、scheduler 置顶检测循环、email 置顶变更通知、config.yaml 自动回写
- **文档**：README/CHANGELOG v1.3.0 更新

---

### v1.2.0 (2026-07-29)

### 核心变化
- **场景二原帖截图嵌入**：放弃 API 文本提取，改用 Playwright 截取 B站动态页面，base64 嵌入邮件
  - **截图时机**：入库时截图（`monitor_scene2` 发现新帖后立即截），邮件端只读已有文件组装，零等待
  - 新增 `screenshotter.py`：Playwright 异步截图模块，照搬 [BTCE3.0](https://github.com/X-tong2568/BTCE) 成熟配置（Chromium headless + 1080×1920 + 2x Retina）
  - 确定性文件名 `dynamic_{id}.png`：入库时生成，邮件端根据 `item_id` 拼接路径查找
  - `config.py` 新增 `ScreenshotConfig`（enabled / max_per_batch / save_dir）
  - `email_notifier.py` 新增 `_embed_screenshot()` base64 嵌入工具函数，builder 自动推导截图路径
  - 截图优先、文本降级：截图文件存在则嵌入 base64，否则走原有文本+富内容渲染
  - Browser 管理：启动/定期重启/优雅退出，独立 context 截图避免状态干扰
  - 零 Cookie：B站动态页 `t.bilibili.com/{id}` 公开可访问

### 文件清单
| 文件 | 变化 |
|------|------|
| `screenshotter.py` | **新增** — Playwright 截图模块 |
| `monitor_scene2.py` | 修改 — 入库时调用截图器 |
| `config.py` | 修改 — 新增 `ScreenshotConfig` |
| `config.yaml` | 修改 — 新增 `screenshot` 配置段 |
| `config.example.yaml` | 修改 — 同上 |
| `email_notifier.py` | 修改 — 截图嵌入 + 路径推导 + 降级逻辑 |
| `main.py` | 修改 — 截图器在 Scene2Monitor 之前初始化 |
| `requirements.txt` | 修改 — 新增 `playwright>=1.40.0` |

### XTong 的贡献
- **需求**：场景二原帖文本提取不稳定，改为 Playwright 截图嵌入邮件
- **架构决策**：截图放在入库时而非发邮件时，邮件端零等待
- **部署**：确认服务器已有 Playwright，指定截图参数 1080×1920 + 2x Retina

### Claude (AI Assistant) 的贡献
- **实现**：`screenshotter.py` 截图模块（搬运 [BTCE](https://github.com/X-tong2568/BTCE) 配置）、monitor_scene2 入库截图、email_notifier 截图嵌入+路径推导+降级逻辑
- **配置**：`ScreenshotConfig` 数据类 + config.yaml 截图配置段
- **文档**：README/CHANGELOG v1.2.0 更新

---

### v1.1.1 补丁 (2026-07-28)

- **fix**: 场景二原帖正文提取改用 `rich_text_nodes` 权威数据源，修复部分动态 `desc.text` 为空导致邮件原帖卡片无正文的问题

---

## v1.1.1 (2026-07-27)

### 核心变化
- **原帖富内容渲染**：场景二邮件原帖上下文卡片从纯文本升级为富内容渲染
  - `bili_client.py` 新增 `_extract_post_rich_content()`：从 `rich_text_nodes` 提取表情、`major.draw` 提取图片、`major.archive` 提取视频信息，序列化为与 `render_comment_html` 兼容的 JSON
  - `database.py` `monitored_items` 新增 `post_rich_content TEXT` 列
  - `email_notifier.py` 原帖区域改用 `render_comment_html()` 渲染（表情 `<img>` + 图片 flex 容器），视频独立渲染为封面+标题+链接卡片
- 渲染模板复用：帖子富内容 JSON 结构与评论 rich_content 完全兼容，无需新写渲染函数

### XTong 的贡献
- **需求**：场景二原帖需展示表情、图片、视频，不能只看文字

### Claude (AI Assistant) 的贡献
- **实现**：`_extract_post_rich_content()` 提取函数、全链路数据传递、富内容邮件渲染、视频卡片 CSS
- **文档**：README/CHANGELOG v1.1.1 更新

---

## v1.1.0 (2026-07-27)

### 核心变化

#### 场景二邮件增强
- **原帖上下文卡片**：`bili_client.py` `get_topic_cards()` 不再丢弃帖子正文，从 `modules.module_dynamic.desc.text` 提取并入库到 `monitored_items.post_content` 新列。邮件渲染时展示为暖黄底色上下文卡片（超长截断500字符），让收件人看懂UP主在回复什么帖子
- **批量合并发送**：场景二互动从逐条即时发送改为队列合并。新增 `_scene2_batch_notify_loop` 调度循环（默认每180秒），将所有待通知场景二互动合并为一封「话题互动汇总」邮件，避免连发多封
- **场景一不受影响**：`_immediate_notify_loop` 改为仅处理场景一，继续逐条即时发送

#### 其他修复
- **scheduler `_get_item_type` 修复**：从永远返回空串改为真正查询 `monitored_items` 表，确保邮件中「作品页面」链接正确区分视频/动态
- **数据库增强**：`get_unnotified_immediate()` 新增 `scene` 可选过滤参数；新增 `get_item_post_content()` 和 `get_item_type()` 查询方法

#### 配置变更
- `intervals` 新增 `scene2_batch_seconds: 180`（场景二批量发送间隔，可调）

### XTong 的贡献
- **需求**：场景二邮件需展示原帖内容 + 批量合并发送

### Claude (AI Assistant) 的贡献
- **实现**：原帖正文全链路（提取→入库→邮件渲染）、场景二批量合并（scheduler + email_notifier + database）、`_get_item_type` 修复
- **文档**：README v1.0.3 版本演进表、CHANGELOG 更新

---

## v1.0.2 (2026-07-25)

### 核心变化
- **Priority 即时推送**：置顶动态发现新互动后立即发送邮件，不再走 30s 队列等待
- **`insert_interaction` 返回值改进**：从 `bool` 改为 `int | None`（返回新行 ID），新增 `get_interaction_by_id()` 供即时推送查询
- **架构调整**：`Scene1Monitor` 注入 `Notifier` 引用，priority 轮询结束后直接调 `send_immediate` 发邮件
- 普通场景1/场景2 互动保持不变，继续走 30s 定时扫库
- 日报机制不变

### XTong 的贡献
- **需求**：提出 priority 发现后立即推送，不走队列等待

### Claude (AI Assistant) 的贡献
- **实现**：`database.py` 返回值改造、`monitor_scene1.py` 即时推送逻辑、`main.py` 构造顺序调整
- **文档**：README v1.0.2 版本演进表、CHANGELOG 更新

---

## v1.0.0 (2026-07-23) — 正式版

### 核心变化
- **富媒体邮件渲染**：`email_notifier.py` 新增 `render_comment_html()`，将 B站评论区的表情占位符(如 `[表情包_示例]`)替换为 `<img>` 标签、评论配图以 flex 容器展示、jump_url 超链接转为可点击链接，全部使用 `https://` 完整 URL 确保邮件客户端正常加载
- **随机主题系统**：`random_theme()` 替代旧的 `random_theme_colors()`，色相 0-360 完全随机；4 种顶栏纹理(星河/月辉/流光/涟漪)纯 CSS 实现；3 档卡片阴影深度；4 种分割线样式
- **邮件预览模式**：`python email_notifier.py --preview` 生成 6 封不同主题的测试邮件到 `sent_emails/`，文件名含纹理名+色值便于对比
- **数据库迁移**：`interactions` 表新增 `rich_content TEXT` 列，自动检测并迁移旧库
- **数据解析扩展**：`parse_comment()` 和 `parse_sub_comment()` 新增 `rich_content` 字段(JSON含 emote/pictures/jump_url)
- **footer 优化**：分割线改为主题色渐变，UP主名用 `「」` 墙角括号，追踪行显示色值

### Claude (AI Assistant) 的贡献
- **代码实现**：`render_comment_html()` 富媒体渲染函数、完整随机主题系统(纹理CSS/氛围色板/光影/分割线)、数据库迁移逻辑、`rich_content` 全链路数据传递
- **纹理设计**：4 种纯 CSS 纹理图案，多轮迭代优化可见性和美观度
- **文档更新**：README v1.0.0 版本号、版本演进表、CHANGELOG

---

## v0.3.0 (2026-07-23)

### 核心变化
- **代理支持**：`bili_client.py` 的 `ClientSession` 新增 `trust_env=True`，配合服务器 Mihomo 代理绕过 B站风控
- **Priority 独立高频轮询**：置顶动态享受 **5s** 独立轮询通道，不参与降级，且始终只有一条
- **置顶评论检测**：处理评论 API 的 `top_replies` 字段（置顶评论），之前只处理 `replies` 导致漏检
- **AV 动态 oid 修复**：视频类动态优先用 `aid` 作为评论区 oid，而非动态自身的 `comment_id_str`
- **归档逻辑修复**：`first_seen_at` 统一用发现时间，旧内容(>24h)从 L2 起步，不再秒归档
- **评论翻页修复**：优先检查 `cursor.is_end`，防止 `pagination_reply` 为空时误翻页
- **Priority 切换自动清理**：更新 config 后自动清除旧 priority 标记，确保始终只有一条

### 技术细节
- 服务器部署 Mihomo v1.19.29 + cron 每小时重启（节点轮换 + 订阅刷新）
- 双层保护：120s 健康检查 + 1 小时定时重启

### XTong 的贡献
- **运维**：提供梯子订阅链接与配置，服务器部署验证
- **需求**：Priority 高频轮询、置顶评论检测、L0 唯一性约束

### Claude (AI Assistant) 的贡献
- **实现**：全部 Python 代码修复（bili_client / database / monitor_scene1 / monitor_scene2 / scheduler / config）
- **部署**：Mihomo 安装配置、systemd 服务、cron 定时重启
- **调试**：hysteria2 TLS 兼容性、SS/Trojan 节点稳定性、B站 API 字段分析（top_replies）

---

## v0.2.0 (2026-07-22)

### 核心变化
- **代理支持**：`bili_client.py` 的 `ClientSession` 新增 `trust_env=True`，自动读取 `HTTPS_PROXY`/`HTTP_PROXY` 环境变量
- **服务器部署**：配合 Mihomo（Clash Meta）代理客户端，B站 API 请求通过梯子节点转发，绕过服务器 IP 风控
- **代理配置**：Mihomo 仅对 B站域名走代理，其他流量直连，不浪费梯子流量
- **测试工具**：`tools/test_proxy.py` 代理连通性测试脚本

### 技术细节
- 服务器部署 Mihomo v1.19.29，使用 VLESS 节点（TCP 协议兼容 Python SSL）
- 代理监听 `127.0.0.1:7890`（HTTP + SOCKS5 混合端口），仅本机使用
- `systemd` 管理 Mihomo 服务，开机自启

### XTong 的贡献
- **运维**：提供梯子订阅链接，服务器部署与验证

### Claude (AI Assistant) 的贡献
- **部署**：Mihomo 安装、配置、systemd 服务搭建
- **实现**：`bili_client.py` 代理支持、代理策略规则配置
- **调试**：hysteria2 TLS 兼容性问题排查，切换 VLESS 节点解决
- **工具**：`tools/test_proxy.py` 代理测试脚本

---

## v0.2.0 (2026-07-22)

### 核心变化
- **零Cookie架构**：彻底移除 `bilibili-api-python` 依赖，改用 `aiohttp` 直连 B站公开 API
- **WBI 签名鉴权**：自动从 nav 接口获取密钥，对评论 API 做 WBI 签名验证
- **评论获取**：`x/v2/reply/wbi/main`（主评论）+ `x/v2/reply/reply`（子评论），游标翻页
- **空间动态发现**：buvid3（自动获取） + WBI 签名，零登录态可用
- **话题发现**：`x/polymer/web-dynamic/v1/feed/topic` + WBI 签名，零 Cookie 可用
- **发现层降级**：空间动态 API 失败时自动降级为 WBI 视频搜索
- **依赖精简**：`bilibili-api-python` → 仅需 `aiohttp + aiosqlite + pyyaml`
- **场景一子评论**：新增 "UP觉得很赞" 检测
- **邮件追踪**：每封邮件末尾追加识别码（comment_id + scene），便于调试
- **邮件留档**：已发送邮件自动保存至 `sent_emails/` 目录
- **免责声明**：新增免责声明
- **测试**：`tests/test_bili_client.py`（4 个单测 + 4 个集成测试）

### XTong 的贡献
- **需求**：推动零 Cookie 方案，提供 `bili-api-guide.md` 技术文档
- **调研**：发现开源参考项目 `Bilibili-load-comment`，验证方案可行性
- **设计**：分级策略、预测窗口机制、通知方案、架构审核
- **测试**：API 调试、端到端验证、邮件接收确认

### Claude (AI Assistant) 的贡献
- **架构**：模块设计、WBI 签名实现、API 适配层
- **实现**：重写 `bili_client.py`（纯 aiohttp + WBI），更新 monitors/scheduler/main
- **文档**：bili-api-guide.md、README.md、CHANGELOG.md
- **工具**：`tests/test_bili_client.py` 单元测试 + 集成测试

---

## v0.1.0 (2026-07-14)

### 功能
- 场景一：UP主自身作品评论监测（主评论 + 楼中楼 + UP觉得很赞）
- 场景二：话题互动监测（两阶段轮询 + 预测窗口）
- 邮件通知：即时推送 + 每日 22:00 日报汇总
- 分级轮询：Level 1 (120s/24h) → Level 2 (600s/120h) → 归档
- 子评论翻页：最多翻 5 页子评论，确保不漏 UP主 的深层回复
- 基于实际发布时间（pub_ts）的精确分级
- HSL 随机配色邮件模板
- SQLite 持久化 + WAL 模式 + 断点续跑

### XTong 的贡献
- **需求**：项目定位、监测场景设计、BTCE3.0 项目参考
- **设计**：分级策略、预测窗口机制、通知方案、架构审核
- **测试**：API 调试、端到端验证、邮件接收确认
- **部署**：服务器环境准备、Cookie 管理

### Claude (AI Assistant) 的贡献
- **架构**：模块设计、数据库 Schema、API 封装
- **实现**：全部 Python 代码（config / database / bili_client / monitor_scene1 / monitor_scene2 / scheduler / email_notifier / logger / retry）
- **文档**：DESIGN.md、PLAN.md、README.md
- **工具**：社区 API 调研、bilibili-api-python 适配、子评论 parent 查找表实现
