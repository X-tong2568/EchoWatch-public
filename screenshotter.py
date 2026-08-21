# screenshotter.py
"""EchoWatch 动态截图模块 —— 使用 Playwright 截取 B站动态页面，供邮件嵌入。

完全参考 BTCE3.0 的成熟截图方案：
- Chromium headless + 1080×1920 + 2x Retina
- 独立 context 截图，避免页面状态互相干扰
- 隐藏 B站顶栏和固定元素后全页截图
- 定期重启浏览器防内存泄漏
"""

import asyncio
import os
import re
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

from logger_config import logger

# 截图代理：与 API 层同一出口，从环境变量读取（服务器 PM2 已配 HTTPS_PROXY=http://127.0.0.1:7890）
# Chromium 不保证读环境变量，必须显式传给 context，否则机房 IP 直连会被 B站 412 风控拦截
SCREENSHOT_PROXY = (
    os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
)
# 补齐 scheme：环境变量可能是 "127.0.0.1:7890" 无协议头写法
if SCREENSHOT_PROXY and "://" not in SCREENSHOT_PROXY:
    SCREENSHOT_PROXY = f"http://{SCREENSHOT_PROXY}"

# ----------------------------------------------------------
# 浏览器启动参数（照搬 BTCE3.0 config.py BROWSER_CONFIG）
# ----------------------------------------------------------
BROWSER_CONFIG = {
    "headless": True,
    "args": [
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--memory-pressure-off",
        "--max_old_space_size=4096",
    ],
}

# 截图参数
VIEWPORT_WIDTH = 700           # 匹配动态卡片宽度（非手机全屏）
VIEWPORT_HEIGHT = 1200
# 动态ID白名单：纯数字动态ID 或 av+数字（空间动态API风控降级路径产物），防路径穿越写入异常文件
_DYNAMIC_ID_RE = re.compile(r"^(av)?\d+$")
DEVICE_SCALE_FACTOR = 2        # 2x Retina 高清
BROWSER_RESTART_INTERVAL = 50  # 每 50 次截图重启浏览器
# 动态卡片 CSS 选择器（照搬 BTCE3.0）
CARD_SELECTOR = '.bili-dyn-item, [class*="dyn-card"]'


class Screenshotter:
    """B站动态截图器，管理 Playwright 浏览器生命周期。"""

    def __init__(self, save_dir: str):
        """save_dir: 截图保存目录（通常是 sent_emails/）"""
        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._browser = None
        self._context = None
        self._shot_count = 0

    # ----------------------------------------------------------
    # 浏览器生命周期
    # ----------------------------------------------------------

    async def start(self):
        """启动 Chromium headless 浏览器"""
        logger.info(f"启动截图浏览器... (代理: {SCREENSHOT_PROXY or '直连'})")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(**BROWSER_CONFIG)
        self._context = await self._browser.new_context()
        logger.info("截图浏览器就绪")

    async def stop(self):
        """关闭浏览器，释放资源"""
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.error(f"关闭截图浏览器异常: {e}")
        self._context = None
        self._browser = None
        self._playwright = None

    async def _restart_if_needed(self):
        """定期重启浏览器，防止内存泄漏"""
        self._shot_count += 1
        if self._shot_count % BROWSER_RESTART_INTERVAL == 0:
            logger.info("定期重启截图浏览器 (%d 次)", self._shot_count)
            await self.stop()
            await asyncio.sleep(2)
            await self.start()

    # ----------------------------------------------------------
    # 核心截图方法
    # ----------------------------------------------------------

    async def take_dynamic_screenshot(self, dynamic_id: str) -> Optional[str]:
        """
        截取 B站单条动态的卡片截图（带重试兜底）。

        失败策略：单次失败（页面超时/加载失败/浏览器崩溃）自动重建浏览器后重试，
        最多 3 次。全部失败返回 None，由调用方降级为文本渲染。

        Args:
            dynamic_id: B站动态 ID（即 item_id）

        Returns:
            截图文件路径（PNG），重试后仍失败返回 None
        """
        # 路径穿越防护：仅接受纯数字动态ID
        if not isinstance(dynamic_id, str) or not _DYNAMIC_ID_RE.match(dynamic_id):
            logger.warning(f"非法动态ID，跳过截图: {dynamic_id}")
            return None

        await self._restart_if_needed()

        for attempt in range(3):
            try:
                return await self._take_screenshot_once(dynamic_id)
            except Exception as e:
                logger.warning(f"动态截图失败 第{attempt + 1}次 ({dynamic_id}): {e}")
                if attempt < 2:
                    # 浏览器可能已崩溃/内存泄漏，重建后重试（不重建则后续截图全挂）
                    try:
                        await self.stop()
                    except Exception:
                        pass
                    try:
                        await self.start()
                    except Exception as e2:
                        logger.error(f"截图浏览器重建失败: {e2}")
                        return None
                    # 间歇性 412 风控几秒内恢复，等足时间再重试，避免连续触发
                    await asyncio.sleep(5)
        return None

    async def _seed_cookies(self, page):
        """
        先访问 B站首页种下 buvid 等游客 cookie。

        无 cookie 的无痕浏览器直访动态页会被 B站强制登录遮罩挡住（内容不渲染，
        整页截出来是登录框）。先逛一次首页拿到游客 cookie 即可正常渲染。
        """
        try:
            await page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            # 种 cookie 失败不致命，继续走目标页（可能仍能渲染）
            pass

    async def _take_screenshot_once(self, dynamic_id: str) -> str:
        """
        单次截图尝试：失败抛异常，由外层 take_dynamic_screenshot 负责重试。

        Args:
            dynamic_id: B站动态 ID（即 item_id）

        Returns:
            截图文件路径（PNG）

        Raises:
            Exception: 页面加载/截图过程中的任意失败（goto超时、元素异常等）
        """
        # 独立高 DPI context，避免与其他截图互相干扰；配了代理则显式走代理（绕机房 IP 412 风控）
        context_opts = {"device_scale_factor": DEVICE_SCALE_FACTOR}
        if SCREENSHOT_PROXY:
            context_opts["proxy"] = {"server": SCREENSHOT_PROXY}
        shot_ctx = await self._browser.new_context(**context_opts)
        try:
            page = await shot_ctx.new_page()
            await page.set_viewport_size({
                "width": VIEWPORT_WIDTH,
                "height": VIEWPORT_HEIGHT,
            })

            # 先种游客 cookie，再访问目标页面（顺序不能反）
            await self._seed_cookies(page)

            # 降级路径（空间动态API风控）的 item_id 带 av 前缀，如 av117032272005640：
            # 没有动态页，只能截视频页（www.bilibili.com/video/av{aid}），走全页降级截图
            is_av_fallback = dynamic_id.startswith("av")
            if is_av_fallback:
                url = f"https://www.bilibili.com/video/{dynamic_id}"
            else:
                url = f"https://t.bilibili.com/{dynamic_id}"
            resp = await page.goto(url, wait_until="load", timeout=20000)
            # HTTP 状态检查：风控拦截页（412/403/429）和代理错误页（502/503）几乎
            # 必然以非 200 状态返回，比文字匹配更可靠，命中直接判失败走重试/降级
            if resp is not None and resp.status >= 400:
                raise RuntimeError(f"页面HTTP状态异常({resp.status}): {url}")

            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(1)

            # 412 风控拦截检测：放在 networkidle 之后（页面已稳定），避免拦截页
            # JS 后渲染时漏检。命中或页面被重定向（URL 不再含动态 ID）则抛异常，
            # 走外层重试/降级，绝不把风控页当动态卡片截下来。
            # 视频页会 302 到 BV 号 URL，不按动态ID校验，只检查仍停留在视频页
            risk_hit = await page.evaluate(
                """() => {
                    const bodyText = document.body ? document.body.innerText.slice(0, 3000) : '';
                    return /request was banned|访问异常|访问过于频繁|访问被拦截|请求被拦截|访问受限|Access Denied|Forbidden/i.test(bodyText);
                }"""
            )
            url_ok = f"/{dynamic_id}" in page.url or (
                is_av_fallback and "/video/" in page.url
            )
            if risk_hit or not url_ok:
                raise RuntimeError(f"页面被B站风控拦截 (url={page.url})")
    
            # 隐藏 B站顶栏和固定元素（照搬 BTCE JS 注入）
            await page.evaluate("""
                () => {
                    const selectors = [
                        '.bili-header', '.bili-header-m', '#biliMainHeader',
                        '.international-header', '.primary-channel', '.bili-header__bar',
                        '.bili-header__channel', '.top-header', '.bili-pendant',
                        '.bili-header__banner', '.bili-header__notice'
                    ];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => {
                            el.style.display = 'none';
                        });
                    });
                    document.querySelectorAll('*').forEach(el => {
                        const style = window.getComputedStyle(el);
                        if (style.position === 'fixed' && parseInt(style.zIndex) > 100) {
                            el.style.display = 'none';
                        }
                    });
                }
            """)
            await asyncio.sleep(0.3)
    
            # 照搬 BTCE3.0：定位动态卡片元素，只截卡片区域而非整个网页
            card = page.locator(CARD_SELECTOR).first
            path = str(self.save_dir / f"dynamic_{dynamic_id}.png")
            if await card.count() > 0:
                # 内容校验：卡片无文本且无图片说明页面空白/未渲染（白屏、登录遮罩、
                # 骨架屏），截出来是废图，抛异常走外层重试/降级
                card_text = await card.inner_text()
                card_imgs = await card.locator("img").count()
                if not card_text.strip() and card_imgs == 0:
                    raise RuntimeError(f"卡片内容为空(空白/未渲染): {dynamic_id}")
                await card.scroll_into_view_if_needed()
                await asyncio.sleep(0.3)
                await card.screenshot(path=path)
            else:
                # 降级：找不到卡片元素时全页截图（兼容B站DOM变更），但先校验页面非空白
                body_text = await page.evaluate(
                    "() => document.body ? document.body.innerText.trim() : ''"
                )
                body_imgs = await page.locator("img").count()
                if not body_text and body_imgs == 0:
                    raise RuntimeError(f"页面内容为空(空白/未渲染): {dynamic_id}")
                logger.warning(f"未找到动态卡片元素，降级为全页截图 ({dynamic_id})")
                await page.screenshot(path=path, full_page=True)
            logger.info(f"动态截图: {path}")
            return path
        finally:
            await shot_ctx.close()
