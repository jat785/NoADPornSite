#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NoADPornSite —— 本地视频直链播放器（yt-dlp 后端 + 浏览器前端）

设计要点
--------
1. yt-dlp 只作为 **Python 库** 使用（``import yt_dlp``），不 fork、不调用 CLI。
   需要扩展站点解析时，写 yt-dlp 的 plugin，不要改 yt-dlp 源码。

2. 站点搜索由本文件里的「站点适配器」实现 —— yt-dlp 的 RedTube 解析器
   只支持视频 ID 形式的 URL（``_VALID_URL`` 里没有 search 分支），
   所以搜索必须自己抓。

3. 所有媒体流都经本服务中转（``/api/media``）：
     - 浏览器只需要连 127.0.0.1，**本身不需要任何代理** → 绕开 GFW
     - 同源，彻底没有 CORS 问题
     - 完整透传 HTTP Range，进度条可以随便拖
     - 服务端出口走 PROXY，所以浏览器端不用配代理

4. 媒体中转带 **主机白名单**，避免这个本地服务被恶意网页当成 SSRF 跳板。

启动
----
    python app.py                 # 默认 http://127.0.0.1:8000

环境变量
--------
    ADSKIPER_PROXY         出口代理，默认 http://127.0.0.1:7890（Clash 混合端口）
    ADSKIPER_HOST          监听地址，默认 127.0.0.1
    ADSKIPER_PORT          监听端口，默认 8000
    ADSKIPER_MEDIA_HOSTS   额外的媒体 CDN 域名后缀，逗号分隔
    ADSKIPER_RESOLVE_TTL   直链解析结果缓存秒数，默认 3000（签名 URL 约 2 小时过期）
"""

from __future__ import annotations

import asyncio
import html as html_mod
import os
import random
import re
import shutil
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import hanime as hanime_mod

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"

PROXY: str = os.environ.get("ADSKIPER_PROXY", "http://127.0.0.1:7890").strip()
HOST: str = os.environ.get("ADSKIPER_HOST", "127.0.0.1").strip()
PORT: int = int(os.environ.get("ADSKIPER_PORT", "8000"))
RESOLVE_TTL: int = int(os.environ.get("ADSKIPER_RESOLVE_TTL", "3000"))


def _find_ffmpeg() -> str | None:
    """找 ffmpeg 可执行文件。找不到返回 None。

    **只有 hanime 的「另存为」需要它** —— tube 站的视频是单个 MP4，直接中转即可；
    hanime 是 HLS（实测一部番剧 142 个 AES-128 加密分片），必须有人把它们解密、
    拼接、再封装成一个容器，这件事交给 ffmpeg。
    没装 ffmpeg 时其余功能完全不受影响，只是 hanime 点下载会得到一句明确报错。

    Node 那边用的是 ADSKIPER_NODE_BIN，这里对称地用 ADSKIPER_FFMPEG 指定绝对路径，
    应对「ffmpeg 不在 PATH 里」的情况。
    """
    env = os.environ.get("ADSKIPER_FFMPEG", "").strip()
    if env:
        return env if Path(env).is_file() else None
    return shutil.which("ffmpeg")


# None = 没找到，下载 HLS 时会给出安装提示
FFMPEG: str | None = _find_ffmpeg()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 媒体中转白名单：只有这些域名允许被本服务代理（防 SSRF）。
# 需要支持别的站点时，用 ADSKIPER_MEDIA_HOSTS 环境变量追加，不用改代码。
DEFAULT_MEDIA_SUFFIXES = [
    "rdtcdn.com",           # RedTube CDN（视频 ev-ph / 封面 pix-cdn77 等）
    "phncdn.com",           # PornHub CDN（视频 ev / 封面 pix-fl 等）
    "hanime.tv",            # hanime 站点自身（HLS 播放列表）
    "hanime-cdn.com",       # hanime 封面 / 剧照
    "htv-services.com",     # hanime HLS 的 AES 密钥（ct.htv-services.com）
    "freeanimehentai.net",  # hanime 的公共目录 API
]
_extra = [s.strip().lstrip(".") for s in os.environ.get("ADSKIPER_MEDIA_HOSTS", "").split(",") if s.strip()]
MEDIA_SUFFIXES: tuple[str, ...] = tuple(DEFAULT_MEDIA_SUFFIXES + _extra)

# hanime 的分片 CDN 是一个**很大的轮换域名池** —— 实测抽 41 个播放列表就出现了
# 48 个不同主机，而且全部形如 ``<sub>.htv-<名字>.com``：
#     p32.htv-dragonlands.com      p05.htv-lakshmi.com     p11.htv-odin.com
#     etheirys.htv-zodiark-03.com  etheirys.htv-hydaelyn-13.com  …
# 后缀列表根本列不完，所以这一类走正则。规则刻意收得比较窄：必须是 htv-*.com。
MEDIA_HOST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:[a-z0-9\-]+\.)*htv-[a-z0-9\-]+\.com$", re.I),
)

# 某些 CDN 会强制校验 Referer，缺了直接失败。
#   典型：RedTube 的封面图 CDN ``pix-cdn77.rdtcdn.com`` —— 不带 Referer 就是 **403**
#         PornHub 的视频 CDN ``ev.phncdn.com`` —— 不带 Referer 是 **404**
#         hanime 的几个 CDN 实测**不需要** Referer，但带上也无副作用。
DEFAULT_REFERER_BY_SUFFIX: dict[str, str] = {
    "rdtcdn.com": "https://www.redtube.com/",
    "phncdn.com": "https://www.pornhub.com/",
    "hanime.tv": "https://hanime.tv/",
    "hanime-cdn.com": "https://hanime.tv/",
    "htv-tsukuyomi.com": "https://hanime.tv/",
    "htv-services.com": "https://hanime.tv/",
}


def default_referer_for(url: str) -> str | None:
    """按域名后缀给出该 CDN 期望的 Referer。"""
    host = (urlparse(url).hostname or "").lower()
    for suffix, ref in DEFAULT_REFERER_BY_SUFFIX.items():
        if host == suffix or host.endswith("." + suffix):
            return ref
    return None


# --------------------------------------------------------------------------- #
# 站点适配器
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 关键词处理
# --------------------------------------------------------------------------- #
# 两个站点的搜索都是**多词 AND**：用空格分隔的每个词都必须命中，结果集逐词收窄。
# 实测 RedTube：lesbian 82,103 → lesbian+anal 10,124 → lesbian+milf+anal 2,537。
# 顺序无关（lesbian+anal 与 anal+lesbian 结果完全相同），生造词会被静默忽略。
#
# 所以本站搜索框的"格式"就是：**空格分隔多个词**。

MAX_TERMS = 8


def split_terms(q: str) -> list[str]:
    """把用户输入拆成关键词列表（按空白切分，去空，最多 MAX_TERMS 个）。

    ``+`` 也当分隔符 —— 站点 URL 里的多词就是 ``a+b+c`` 的形式，用户从别处
    复制过来时能直接用。搜索词里出现字面加号在本站场景下没有实际意义。
    """
    return [t for t in re.split(r"[\s+]+", (q or "").strip()) if t][:MAX_TERMS]


def encode_query(q: str) -> str:
    """编成站点 URL 里的搜索串：词之间用 ``+`` 连接（等价于空格）。

    不要直接对整串调用 ``quote()`` —— 那样 ``+`` 会被转义成 ``%2B``，
    变成字面加号，站点就不会按多词 AND 处理了。
    """
    return "+".join(quote(t, safe="") for t in split_terms(q))


class SiteAdapter:
    """一个站点的搜索实现。想加新站点就照抄一个子类。"""

    key: str = ""
    name: str = ""
    base: str = ""
    cookie: str = ""
    supports_search: bool = True

    # "paged"   —— 服务端渲染的列表页，按页抓（RedTube / PornHub）
    # "catalog" —— 一次性拉全量索引，搜索/过滤/分页都在本地做（hanime）
    search_kind: str = "paged"

    # 结果里是否带结构化标签（hanime 有；tube 站只有文本关键词）
    has_tags: bool = False

    @classmethod
    def search_url(cls, q: str, page: int) -> str:
        raise NotImplementedError

    @classmethod
    def browse_urls(cls) -> list[str]:
        """无关键词时的"随便看看"入口：返回本次要抓的列表页 URL。

        tube 站都没有"给我随机视频"的接口，只能在列表页上做文章：
        随机挑分类页 / 随机页码，抓回来再从结果里随机抽若干条。
        空列表表示该站点没有这种列表页（hanime 直接用全量目录抽样）。
        调用方会把多个页面的结果**合并去重后**再抽样，所以多给几个能提高多样性。
        """
        return []

    @classmethod
    def video_page_url(cls, vid: str) -> str:
        """由搜索结果里的 id 拼出视频页 URL（/api/thumb 取 og:image 要用）。"""
        return f"{cls.base}/{vid}"

    @classmethod
    def parse_search(cls, body: str, page: int = 1) -> tuple[list[dict[str, Any]], int | None, str]:
        """返回 (结果列表, 总页数, 结果总数文本)。

        ``page`` 是当前页码，仅用于"翻到最后一页时推断总页数"这类场景
        （PornHub 页面上不直接给出总页数）。
        """
        raise NotImplementedError


# RedTube 搜索结果卡片的解析正则（依据真实页面结构，2026-09 实测）
_CARD_SPLIT = re.compile(r"(?=<li[^>]*data-video-id=)")
_RE_VID = re.compile(r'data-video-id="(\d+)"')
_RE_HREF = re.compile(r'<a\b[^>]*class="[^"]*video-title-text[^"]*"[^>]*>', re.S)
_RE_HREF_IN = re.compile(r'href="([^"]+)"')
_RE_TITLE_ATTR = re.compile(r'title="([^"]*)"')
_RE_TITLE_BLOCK = re.compile(
    r'<a\b[^>]*class="[^"]*video-title-text[^"]*"[^>]*>(.*?)</a>', re.S
)
_RE_DUR = re.compile(r'tm_video_duration[^>]*>\s*([0-9:]+)\s*<')
_RE_VIEWS = re.compile(r"info-views'\s*>\s*([^<]+?)\s*<")
_RE_O_THUMB = re.compile(r'data-o_thumb="([^"]+)"')
_RE_SRC = re.compile(r'data-src="([^"]+)"')
# 卡片里有两个 data-srcset：<source> 是 webp，<img> 是 jpg
_RE_SRCSET_WEBP = re.compile(r'<source[^>]*data-srcset="([^"]+?)\s+1x"', re.S)
_RE_SRCSET_JPG = re.compile(r'<img[^>]*data-srcset="([^"]+?)\s+1x"', re.S)
_RE_MEDIABOOK = re.compile(r'data-mediabook="([^"]+)"')
_RE_UPLOADER = re.compile(r'data-uploader-name="([^"]*)"')
# 页面里嵌的是 JS 对象： pagination: {"totalPages":455,...}  searchCount: "82,103"
_RE_TOTAL_PAGES = re.compile(r'"totalPages"\s*:\s*(\d+)')
_RE_SEARCH_COUNT = re.compile(r'searchCount:\s*"([^"]+)"')


def _unescape(s: str | None) -> str:
    return html_mod.unescape(s).strip() if s else ""


# 列表页上的时长文本："12:34" 或 "1:02:03"
_RE_DUR_TEXT = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")

# 随机推荐里混进 6 秒的剪辑看着像坏了 → 时长能解析出来时要求至少 1 分钟。
# hanime 的目录没有时长字段（duration 为空），不参与这条过滤。
MIN_RANDOM_SECONDS = 60


def _dur_seconds(text: str) -> int | None:
    """把 "12:34" / "1:02:03" 这样的时长文本转成秒；识别不了返回 None。"""
    m = _RE_DUR_TEXT.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _long_enough(item: dict[str, Any]) -> bool:
    """时长未知的一律放行，只挡掉明确太短的。"""
    secs = _dur_seconds(item.get("duration", ""))
    return secs is None or secs >= MIN_RANDOM_SECONDS


# 最近几轮随机推荐已经给过的条目，新一轮尽量避开 ——
# 用户点「刷新」的预期是"换一批"，不是"把同一批重新洗牌"。
# 每站记住最近 6 轮（10 个/轮 = 60 个）。
_recent_shown: dict[str, deque[str]] = {}
RECENT_WINDOW = 60


def _pick_random(
    pool: list[dict[str, Any]], count: int, site_key: str
) -> list[dict[str, Any]]:
    """从候选池里随机抽 count 条，避开最近刚给过的。

    这是"刷新"体验的关键：光把池子做大解决不了问题 ——
    池子越大，任意两次抽样之间的期望重叠虽小，但**连续两次**的重叠
    才是用户真正会看到的。直接排除最近展示过的条目，连续两次的重叠就是 0。
    """
    if not pool:
        return []
    recent = _recent_shown.setdefault(site_key, deque(maxlen=RECENT_WINDOW))
    fresh = [x for x in pool if x["id"] not in recent]
    if len(fresh) < count:
        # 池子快被"避"空了（比如 RedTube 一共就 200 来条）→ 放宽，
        # 宁可重复也不能越刷越少
        fresh = pool
    picks = random.sample(fresh, min(count, len(fresh)))
    recent.extend(x["id"] for x in picks)
    return picks


class RedTubeSite(SiteAdapter):
    # key 必须和 yt-dlp 的 IE_NAME 完全一致（注意是大写驼峰），
    # 这样站点下拉框里的选项才能和搜索适配器对上。
    key = "RedTube"
    name = "RedTube"
    base = "https://www.redtube.com"
    # 年龄确认 cookie，否则拿到的是年龄门页面
    cookie = "accessAgeDisclaimerRT=1; accessRT=1; platform=pc; age_verified=1"

    @classmethod
    def search_url(cls, q: str, page: int) -> str:
        # q 里可以有多个空格分隔的词 → encode_query 会拼成 "a+b+c"，站点按 AND 处理
        return f"{cls.base}/?search={encode_query(q)}&page={page}"

    # 无关键词时能用的列表页。实测 RedTube 的 ?page=N 一律 404（首页也一样），
    # 所以凑不出"随机页码"，只能在这些固定入口里随机挑（六个入口合计约 250 条）。
    BROWSE_PATHS = ("/", "/hot/", "/top/", "/newest/", "/mostviewed/", "/longest/")
    # 单个入口只有 ~40 条，从 40 里抽 10 连续两次平均会撞 2.5 个；
    # 一次抓两个入口合并后再抽，重复率大致减半。
    BROWSE_POOLS_PER_PICK = 2

    @classmethod
    def browse_urls(cls) -> list[str]:
        return [cls.base + p
                for p in random.sample(cls.BROWSE_PATHS, cls.BROWSE_POOLS_PER_PICK)]

    @classmethod
    def parse_search(cls, body: str, page: int = 1) -> tuple[list[dict[str, Any]], int | None, str]:
        """返回 (结果列表, 总页数, 结果总数文本)。"""
        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        for chunk in _CARD_SPLIT.split(body):
            if "video-title-text" not in chunk:
                continue  # 广告位等非视频节点
            # 每块从 <li ...> 起算，但**最后一块会一直延伸到页面末尾**（后面没有
            # 下一个 <li data-video-id> 可供切分），必须按 </li> 截断；否则这一页
            # 尾部的所有 <img> 都会被当成本视频的封面候选（实测凑出 30 个垃圾候选）。
            _end = chunk.find("</li>")
            if _end > 0:
                chunk = chunk[:_end]

            m_vid = _RE_VID.search(chunk)
            if not m_vid:
                continue
            vid = m_vid.group(1)
            if vid in seen:
                continue
            seen.add(vid)

            m_block = _RE_TITLE_BLOCK.search(chunk)
            title = ""
            href = ""
            if m_block:
                tag_start = chunk.rfind("<a", 0, m_block.start() + 1)
                open_tag = chunk[tag_start: m_block.start() + 1]
                m_t = _RE_TITLE_ATTR.search(open_tag)
                if m_t:
                    title = _unescape(m_t.group(1))
                m_h = _RE_HREF_IN.search(open_tag)
                if m_h:
                    href = m_h.group(1)
                if not title:
                    title = _unescape(re.sub(r"<[^>]+>", " ", m_block.group(1)))

            m_dur = _RE_DUR.search(chunk)
            views = _RE_VIEWS.findall(chunk)

            # 收集**所有**封面候选，按优先级排列。
            # 必须这么做：CDN77 上某些视频的 304:171 那档会稳定返回 502（带上
            # Referer 才越过 403 关卡，然后后端报错），但同一视频的 224:126
            # 那档是好的。而 hash 是绑定尺寸签名的，不能自己改尺寸重算，
            # 所以只能把卡片里现成的几档都交给前端逐个回退。
            thumbs: list[str] = []

            def _add_thumb(raw: str | None) -> None:
                u = _unescape(raw)
                if u and u not in thumbs:
                    thumbs.append(u)

            for m in _RE_SRC.finditer(chunk):
                _add_thumb(m.group(1))
            for m in _RE_O_THUMB.finditer(chunk):
                _add_thumb(m.group(1))
            for m in _RE_SRCSET_JPG.finditer(chunk):
                _add_thumb(m.group(1))
            for m in _RE_SRCSET_WEBP.finditer(chunk):
                _add_thumb(m.group(1))

            m_mb = _RE_MEDIABOOK.search(chunk)
            m_up = _RE_UPLOADER.search(chunk)

            results.append({
                "id": vid,
                "title": title or vid,
                "url": f"{cls.base}{href}" if href.startswith("/") else (href or f"{cls.base}/{vid}"),
                "duration": m_dur.group(1) if m_dur else "",
                "views": _unescape(views[0]) if views else "",
                "rating": _unescape(views[1]) if len(views) > 1 else "",
                "thumbnail": thumbs[0] if thumbs else "",
                "thumbnails": thumbs,          # 前端按顺序回退
                "preview": _unescape(m_mb.group(1)) if m_mb else "",
                "uploader": _unescape(m_up.group(1)) if m_up else "",
            })

        m_pages = _RE_TOTAL_PAGES.search(body)
        total_pages = int(m_pages.group(1)) if m_pages else None
        m_count = _RE_SEARCH_COUNT.search(body)
        total_count = _unescape(m_count.group(1)) if m_count else ""
        return results, total_pages, total_count


# --------------------------------------------------------------------------- #
# PornHub
# --------------------------------------------------------------------------- #
# PornHub 搜索页结构（2026-09 实测）：
#   <li class="pcVideoListItem js-pop videoblock videoBox" data-video-id="..." data-video-vkey="...">
#     <a href="/view_video.php?viewkey=XXX" title="标题">
#       <img src="..." data-image="..." data-mediabook="...fb.mp4">
#       <var class="duration">22:25</var>
#     </a>
#     <span class="title"><a href="...">标题</a></span>
#     <div class="usernameWrap"><a title="上传者">
#     <span class="views"><var>134K</var> views</span>
#
# 注意：PornHub 页面上**不提供总页数**，只能靠"下一页按钮是否 disabled"判断末页。

_RE_PH_SPLIT = re.compile(r'(?=<li class="pcVideoListItem)')
_RE_PH_VKEY = re.compile(r'data-video-vkey="([^"]+)"')
_RE_PH_HREF = re.compile(r'href="(/view_video\.php\?viewkey=[^"]+)"')
_RE_PH_TITLE_ATTR = re.compile(r'<a\s+href="/view_video\.php\?viewkey=[^"]*"\s+title="([^"]*)"')
_RE_PH_TITLE_SPAN = re.compile(r'<span class="title">\s*<a[^>]*>(.*?)</a>', re.S)
_RE_PH_DUR = re.compile(r'<var class="duration">\s*([\d:]+)\s*</var>')
_RE_PH_VIEWS = re.compile(r'<span class="views">\s*<var>([^<]+)</var>')
_RE_PH_UPLOADER = re.compile(r'class="usernameWrap">[\s\S]{0,500}?<a[^>]*?title="([^"]*)"')
_RE_PH_IMAGE = re.compile(r'data-image="([^"]+)"')
_RE_PH_SRC = re.compile(r'<img\s+src="(https?://[^"]+)"')
_RE_PH_MEDIABOOK = re.compile(r'data-mediabook="([^"]+)"')
_RE_PH_NEXT_OFF = re.compile(r'class="page_next\s+disabled"')


class PornHubSite(SiteAdapter):
    key = "PornHub"
    name = "PornHub"
    base = "https://www.pornhub.com"
    cookie = "accessAgeDisclaimerPH=1; accessPH=1; platform=pc"

    @classmethod
    def search_url(cls, q: str, page: int) -> str:
        return f"{cls.base}/video/search?search={encode_query(q)}&page={page}"

    # 浏览列表页：排序档位 × 随机页码（实测每页 35~47 条）。
    # 档位是站点自己的缩写：mv=播放最多 / cm=评论最多 / tr=评分最高 / ht=最热。
    BROWSE_SORTS = ("mv", "cm", "tr", "ht")
    # ⚠️ 页码上限不能给太大：实测 o=mv/cm/tr 到 40 页都还在，但 o=ht 第 20 页还行、
    #    第 25 页就 404 了（上限是浮动的）。取 18 留出安全余量 —— 4 档 × 18 页
    #    ≈ 3000 条候选，对"随机推荐"完全够用。
    BROWSE_MAX_PAGE = 18

    @classmethod
    def browse_urls(cls) -> list[str]:
        return [f"{cls.base}/video?o={random.choice(cls.BROWSE_SORTS)}"
                f"&page={random.randint(1, cls.BROWSE_MAX_PAGE)}"]

    @classmethod
    def video_page_url(cls, vid: str) -> str:
        # PornHub 的视频页用的是 viewkey（十六进制串），不是数字 id
        return f"{cls.base}/view_video.php?viewkey={vid}"

    @classmethod
    def parse_search(cls, body: str, page: int = 1) -> tuple[list[dict[str, Any]], int | None, str]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        for chunk in _RE_PH_SPLIT.split(body):
            m_vk = _RE_PH_VKEY.search(chunk)
            if not m_vk:
                continue
            vkey = m_vk.group(1)
            if vkey in seen:
                continue
            seen.add(vkey)

            # 同 RedTube：最后一块会延伸到页面末尾，先按 </li> 截断再取字段
            _end = chunk.find("</li>")
            if _end > 0:
                chunk = chunk[:_end]

            m_href = _RE_PH_HREF.search(chunk)
            title = ""
            m_t = _RE_PH_TITLE_ATTR.search(chunk)
            if m_t:
                title = _unescape(m_t.group(1))
            if not title:
                m_ts = _RE_PH_TITLE_SPAN.search(chunk)
                if m_ts:
                    title = _unescape(re.sub(r"<[^>]+>", " ", m_ts.group(1)))

            thumbs: list[str] = []
            for rx in (_RE_PH_IMAGE, _RE_PH_SRC):
                for m in rx.finditer(chunk):
                    u = _unescape(m.group(1))
                    if u and u not in thumbs:
                        thumbs.append(u)

            m_dur = _RE_PH_DUR.search(chunk)
            m_views = _RE_PH_VIEWS.search(chunk)
            m_up = _RE_PH_UPLOADER.search(chunk)
            m_mb = _RE_PH_MEDIABOOK.search(chunk)

            results.append({
                # 用 viewkey 当 id：视频页 URL 和 /api/thumb 都要它
                "id": vkey,
                "title": title or vkey,
                "url": (cls.base + m_href.group(1)) if m_href else cls.video_page_url(vkey),
                "duration": m_dur.group(1) if m_dur else "",
                "views": _unescape(m_views.group(1)) if m_views else "",
                "rating": "",
                "thumbnail": thumbs[0] if thumbs else "",
                "thumbnails": thumbs,
                "preview": _unescape(m_mb.group(1)) if m_mb else "",
                "uploader": _unescape(m_up.group(1)) if m_up else "",
            })

        # 只有翻到最后一页（下一页按钮 disabled）才能知道总页数
        total_pages = page if _RE_PH_NEXT_OFF.search(body) else None
        return results, total_pages, ""


SITES: dict[str, type[SiteAdapter]] = {
    RedTubeSite.key: RedTubeSite,
    PornHubSite.key: PornHubSite,
}

_SITES_CI: dict[str, type[SiteAdapter]] = {k.lower(): v for k, v in SITES.items()}


def get_site(key: str) -> type[SiteAdapter] | None:
    """按 key 找搜索适配器，大小写不敏感。"""
    return SITES.get(key) or _SITES_CI.get((key or "").lower())


# --------------------------------------------------------------------------- #
# hanime.tv
# --------------------------------------------------------------------------- #
# 形态和上面两个完全不同：Astro 前端 + JSON API，yt-dlp 不支持，取流要过 WASM 签名。
# 详见 hanime.py。这里只做一个"外壳"，把它的目录接口适配成站点选择器能认的样子。
# 目录接口不可用时（站点改版等）会返回空结果，不影响另外两个站。


class HanimeSite(SiteAdapter):
    key = "hanime"
    name = "hanime.tv"
    base = "https://hanime.tv"
    supports_search = True
    search_kind = "catalog"     # 一次性拉全量索引，本地过滤
    has_tags = True             # 带回结构化标签

    @classmethod
    def video_page_url(cls, vid: str) -> str:
        return f"{cls.base}/videos/hentai/{vid}"


SITES[HanimeSite.key] = HanimeSite
_SITES_CI[HanimeSite.key.lower()] = HanimeSite


# --------------------------------------------------------------------------- #
# 支持的站点
# --------------------------------------------------------------------------- #

# key 必须**严格等于** yt-dlp 的 IE_NAME（RedTube / PornHub 是大写驼峰）。
# hanime 没有 yt-dlp extractor，所以把自己的 resolver 标成 custom。
# domain 只用于界面展示。
#
# ⚠️ /api/resolve 对前两者是把 URL 交给 yt-dlp，对 hanime 走自研取流；
#    粘贴任何 yt-dlp 支持的链接也都能播 —— 这份清单只决定"站点按钮上能选什么"。
SUPPORTED_SITES: list[dict[str, str]] = [
    {"key": "RedTube", "name": "RedTube", "domain": "redtube.com", "resolver": "ytdlp"},
    {"key": "PornHub", "name": "PornHub", "domain": "pornhub.com", "resolver": "ytdlp"},
    {"key": "hanime", "name": "hanime.tv", "domain": "hanime.tv", "resolver": "hanime"},
]

_catalog: list[dict[str, Any]] | None = None


def site_catalog() -> list[dict[str, Any]]:
    """前端站点选择器用的清单。首次调用后缓存。"""
    global _catalog
    if _catalog is not None:
        return _catalog

    # 校验 IE_NAME 是否真的存在于 yt-dlp（升级 yt-dlp 后若某站被移除会体现在这里）
    try:
        from yt_dlp.extractor import gen_extractor_classes
        known = {getattr(c, "IE_NAME", None) for c in gen_extractor_classes()}
    except Exception:  # noqa: BLE001
        known = None

    rows: list[dict[str, Any]] = []
    for s in SUPPORTED_SITES:
        adapter = get_site(s["key"])
        custom = s.get("resolver") == "hanime"
        rows.append({
            "key": s["key"],
            "name": s["name"],
            "domain": s["domain"],
            "resolver": s.get("resolver", "ytdlp"),
            # hanime 不由 yt-dlp 解析，别拿 IE_NAME 去校验它
            "available": True if custom else ((s["key"] in known) if known else True),
            "supports_search": adapter is not None,
            # 前端据此决定搜索栏形态：catalog 类站点要显示标签多选
            "search_kind": getattr(adapter, "search_kind", "paged"),
            "has_tags": bool(getattr(adapter, "has_tags", False)),
        })
    # 能搜关键词的排前面
    rows.sort(key=lambda x: (not x["supports_search"], x["name"].lower()))
    _catalog = rows
    return rows


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #

class TTLCache:
    def __init__(self, ttl: int, maxsize: int = 256) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self._d: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.time() - ts > self.ttl:
            self._d.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any) -> None:
        if len(self._d) >= self.maxsize:
            oldest = min(self._d, key=lambda k: self._d[k][0])
            self._d.pop(oldest, None)
        self._d[key] = (time.time(), val)

    def drop(self, key: str) -> None:
        self._d.pop(key, None)


resolve_cache = TTLCache(ttl=RESOLVE_TTL)
search_cache = TTLCache(ttl=300, maxsize=64)
# 无关键词浏览页的解析结果。PornHub 的"随机页码"空间很大（4 档 × 18 页），
# 单页缓存 + 每站一个累积池都要放得下，所以 maxsize 给宽一点，避免互相挤掉。
# TTL 给 10 分钟：既能让连点「刷新」很快，又不至于十几次之后还是同一批。
browse_cache = TTLCache(ttl=600, maxsize=128)


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #

def _make_client(use_proxy: bool = True) -> httpx.AsyncClient:
    kw: dict[str, Any] = dict(
        timeout=httpx.Timeout(20.0, read=120.0),
        follow_redirects=True,
        headers={"User-Agent": UA},
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        # 关键：不要读环境变量 / 系统代理设置。
        # httpx 默认 trust_env=True，会通过 getproxies() 读到 Windows 注册表里的
        # 系统代理（Clash 的 127.0.0.1:7890），导致连 127.0.0.1 自己的服务都被
        # 丢给 Clash，返回 502 Bad Gateway。这里显式关掉，出口代理只由 PROXY 决定。
        trust_env=False,
    )
    if use_proxy and PROXY:
        kw["proxy"] = PROXY
    return httpx.AsyncClient(**kw)


client: httpx.AsyncClient = _make_client(use_proxy=True)


def host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if any(s == host or host.endswith("." + s) for s in MEDIA_SUFFIXES):
        return True
    # hanime 那种轮换域名池（htv-*.com）
    return any(p.match(host) for p in MEDIA_HOST_PATTERNS)


# --------------------------------------------------------------------------- #
# 上游失败的诊断信息
# --------------------------------------------------------------------------- #

# 上游返回"拒绝"类状态码（403 / 410 / 451 / 429…）时，**原因几乎总在 body 里**，
# 状态码本身没有信息量。实测踩过这个坑：PornHub 对某个出口 IP 稳定返回 410，
# 光看 "410 Gone" 完全分不清是「地区不提供服务」「被风控判定为机器人」
# 「Cloudflare 挑战」还是「本地代理在拦」—— 而那三种的应对方式完全不同。
_ERR_SNIPPET_LIMIT = 400


def _body_snippet(text: str, limit: int = _ERR_SNIPPET_LIMIT) -> str:
    """把上游 body 压成一行可读纯文本。

    去掉 script/style 和标签，HTML 反转义，空白折叠 —— 因为 Cloudflare 的页面
    和站点的提示页都是几百 KB 的 HTML，原样塞进错误信息没法看。
    """
    if not text:
        return ""
    t = re.sub(r"<(script|style|noscript)\b.*?</\1\s*>", " ", text, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_mod.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + "…" if len(t) > limit else t


def upstream_error_detail(exc: Exception) -> str:
    """把上游异常变成「状态码 + 关键响应头 + body 摘要」。

    对非 HTTP 错误（连接超时、DNS 失败等）就退回原始的异常文本。
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return str(exc)

    bits = [f"HTTP {resp.status_code} {resp.reason_phrase}".strip()]
    # 这几个头能直接说明"是谁在拒绝你"：
    #   server: cloudflare  -> 是 CDN 层拦的，不是站点本身
    #   cf-mitigated        -> Cloudflare 明确标注了拦截类型（如 challenge）
    for h in ("server", "cf-mitigated", "cf-ray"):
        if v := resp.headers.get(h):
            bits.append(f"{h}={v}")

    try:
        body = resp.text
    except Exception:  # noqa: BLE001
        # 流式响应还没读、或已被关闭 —— 拿不到 body 就没必要硬来
        body = ""

    snip = _body_snippet(body)
    if snip:
        bits.append(f"上游说：{snip}")
    elif not body:
        bits.append("（上游 body 为空 —— 通常是代理层直接拒绝，而不是站点回的）")

    return " ｜ ".join(bits)


# --------------------------------------------------------------------------- #
# hanime.tv 运行时状态
# --------------------------------------------------------------------------- #
# 签名助手是个 Node 常驻子进程，加上 vendor 需要联网抓取 —— 都是**懒加载**：
# 只有真正用到 hanime 取流时才初始化。目录接口不需要它。

# 注意：这里用**两把独立的锁**。之前用同一把 Lock 时，
# _hanime_signer_instance() 持锁期间又去调 _hanime_http()（也要同一把锁），
# 非可重入锁直接把自己锁死，表现为请求永远超时。
_hanime_http_lock = threading.Lock()
_hanime_signer_lock = threading.Lock()
_hanime_client: httpx.Client | None = None
_hanime_signer: hanime_mod.Signer | None = None


def _hanime_http() -> httpx.Client:
    """hanime 用的同步客户端（签名助手是同步的，所以这条链路整体同步）。"""
    global _hanime_client
    with _hanime_http_lock:
        if _hanime_client is None:
            kw: dict[str, Any] = dict(
                timeout=httpx.Timeout(30.0, read=120.0),
                follow_redirects=True,
                headers={"User-Agent": hanime_mod.UA},
                trust_env=False,
            )
            if PROXY:
                kw["proxy"] = PROXY
            _hanime_client = httpx.Client(**kw)
        return _hanime_client


def _hanime_signer_instance() -> hanime_mod.Signer:
    """拿到（必要时启动）签名助手。失败会抛 hanime.SignerError。

    进程启动和 vendor 抓取都比较慢，所以放在锁外做；
    锁只用来保证不会同时起两个助手。
    """
    global _hanime_signer
    with _hanime_signer_lock:
        if _hanime_signer is not None:
            return _hanime_signer

    vendor, _url = hanime_mod.fetch_vendor(_hanime_http())
    signer = hanime_mod.Signer(vendor)

    with _hanime_signer_lock:
        if _hanime_signer is None:
            _hanime_signer = signer
            return signer
        signer.close()          # 竞争输了，把自己起的那个关掉
        return _hanime_signer


def _hanime_ready() -> bool:
    """探测 hanime 支持是否可用（目录接口通不通）。"""
    try:
        hanime_mod.load_catalog(_hanime_http())
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# yt-dlp 解析
# --------------------------------------------------------------------------- #

def _extract_sync(url: str) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 1,
        "http_headers": {"User-Agent": UA},
    }
    if PROXY:
        opts["proxy"] = PROXY
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _normalize_info(info: dict[str, Any], source_url: str) -> dict[str, Any]:
    """把 yt-dlp 的 info 字典整理成前端好用的形状。"""
    formats: list[dict[str, Any]] = []
    for f in info.get("formats") or []:
        u = f.get("url")
        if not u:
            continue
        # 只要渐进式 MP4：CDN 没给 CORS 头，HLS 在浏览器里没法用 hls.js
        if f.get("ext") != "mp4":
            continue
        # 注意：RedTube 的渐进式 MP4 条目 yt-dlp 给的 vcodec 是 None（未知），
        # 只有字符串 'none' 才代表"纯音频"。所以这里不能把 None 一起排除。
        if f.get("vcodec") == "none":
            continue
        if "m3u8" in u or str(f.get("protocol", "")).startswith("m3u8"):
            continue
        h = f.get("height") or 0
        formats.append({
            "height": h,
            "label": f"{h}p" if h else (f.get("format_id") or "?"),
            "url": u,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
        })

    # 同清晰度去重（保留第一个），按高度从高到低
    best: dict[int, dict[str, Any]] = {}
    for f in formats:
        best.setdefault(f["height"], f)
    formats = sorted(best.values(), key=lambda x: x["height"], reverse=True)

    if not formats:
        raise HTTPException(502, "yt-dlp 没有返回可用的 MP4 直链")

    # 这里**故意不返回 tags**：播放页的标签行只服务 hanime（它有站方给定的结构化 tag，
    # 一条 2~27 个）。tube 站的标签要么没有（RedTube 两个字段都是空的），要么是一堆
    # 自由关键词（实测 PornHub 会同时给 11 个 categories + 16 个 tags，语言还很杂），
    # 铺在标题下方只会变成噪音。前端 renderPlayerTags() 拿不到东西就整行隐藏，
    # 所以哪天想让 tube 站也显示，在这儿加上字段即可。
    return {
        "id": info.get("id"),
        "title": info.get("title") or info.get("id") or "未命名",
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or source_url,
        "description": (info.get("description") or "")[:800],
        "formats": formats,
    }


async def resolve(url: str, refresh: bool = False) -> dict[str, Any]:
    key = url.strip()
    if not refresh:
        cached = resolve_cache.get(key)
        if cached:
            return cached

    try:
        info = await run_in_threadpool(_extract_sync, key)
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(502, f"yt-dlp 解析失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"解析异常：{type(exc).__name__}: {exc}") from exc

    data = _normalize_info(info, key)
    resolve_cache.set(key, data)
    return data


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await client.aclose()


app = FastAPI(title="NoADPornSite", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(500, "index.html 缺失")
    return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8")


@app.get("/api/sites")
async def api_sites() -> JSONResponse:
    """成人站清单。supports_search=True 的才有列表页搜索适配器。"""
    sites = await run_in_threadpool(site_catalog)
    return JSONResponse({
        "count": len(sites),
        "search_capable": [s["key"] for s in sites if s["supports_search"]],
        "sites": sites,
        # 没找到就是 None。前端据此判断 hanime 的「下载」能不能用
        # （tube 站是单个 MP4，不需要 ffmpeg）
        "ffmpeg": FFMPEG,
    })


@app.get("/api/search")
async def api_search(
    q: str = Query("", description="关键词；catalog 类站点可留空，只用标签过滤"),
    page: int = Query(1, ge=1),
    site: str = Query("RedTube"),
    tags: str = Query("", description="逗号分隔的标签，精确匹配（AND）；仅 catalog 类站点支持"),
    sort: str = Query("", description="catalog 类站点的排序：views / new"),
) -> JSONResponse:
    adapter = get_site(site)
    if adapter is None:
        capable = "、".join(s.name for s in SITES.values() if s.supports_search) or "（暂无）"
        # 还在站点清单里、但没写搜索适配器 → 400；根本不在清单里 → 404
        known = any(s["key"] == site for s in await run_in_threadpool(site_catalog))
        if known:
            raise HTTPException(400, f"{site} 还没有搜索适配器，只能播放链接。"
                                     f"目前能搜关键词的站点：{capable}")
        raise HTTPException(404, f"站点不在列表里：{site}。"
                                 f"目前能搜关键词的是 {capable}；"
                                 f"其它网站的链接可以直接粘进搜索框播放。")
    if not adapter.supports_search:
        raise HTTPException(400, f"{adapter.name} 不支持搜索")

    term_list = split_terms(q)
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]

    if getattr(adapter, "search_kind", "paged") == "catalog":
        return await _search_catalog(adapter, q, term_list, tag_list, page, sort)

    if not term_list:
        raise HTTPException(400, "请输入搜索关键词")
    # 归一化后的关键词串（多词用 + 连接），同时用作缓存键，避免空格差异产生多份缓存
    norm_q = "+".join(term_list)

    url = adapter.search_url(norm_q, page)
    cache_key = f"{site}|{norm_q}|{page}"
    cached = search_cache.get(cache_key)
    if cached:
        return JSONResponse(cached)

    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if adapter.cookie:
        headers["Cookie"] = adapter.cookie

    try:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            502, f"搜索请求失败（{adapter.name}）：{upstream_error_detail(exc)}"
        ) from exc

    items, total_pages, total_count = adapter.parse_search(r.text, page)
    payload = {
        "site": site,
        "site_name": adapter.name,
        "query": q,
        "terms": term_list,      # 前端据此提示"这几个词是同时匹配"
        "term_count": len(term_list),
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "count": len(items),
        "results": items,
    }
    search_cache.set(cache_key, payload)
    return JSONResponse(payload)


async def _search_catalog(
    adapter: type[SiteAdapter],
    q: str,
    term_list: list[str],
    tag_list: list[str],
    page: int,
    sort: str,
) -> JSONResponse:
    """catalog 类站点（hanime）的搜索：目录全量拉一次，过滤/分页在本地做。

    和 tube 站的差别：
      * 关键词、标签都可以为空（可以只按标签浏览）
      * 标签是**精确匹配 + AND**（结构化元数据，不是自由文本）
      * 目录接口挂掉时返回空结果而不是 500，避免把整个前端拖垮
    """
    if not term_list and not tag_list:
        # 什么都不给就当作"浏览全部"，按热度排
        sort = sort or "views"

    def _work() -> dict[str, Any]:
        c = _hanime_http()
        hanime_mod.load_catalog(c)
        return hanime_mod.filter_catalog(
            q=" ".join(term_list),
            tags=tag_list,
            page=page,
            sort=sort,
        )

    try:
        data = await run_in_threadpool(_work)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"hanime 目录获取失败：{upstream_error_detail(exc)}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"hanime 目录不可用：{type(exc).__name__}: {exc}") from exc

    payload = {
        "site": adapter.key,
        "site_name": adapter.name,
        "query": q,
        "terms": term_list,
        "term_count": len(term_list),
        "tags": tag_list,
        "page": data["page"],
        "total_pages": data["total_pages"],
        "total_count": str(data["total"]),
        "count": len(data["results"]),
        "results": data["results"],
    }
    return JSONResponse(payload)


@app.get("/api/hanime/tags")
async def api_hanime_tags(limit: int = Query(200, ge=1, le=1000)) -> JSONResponse:
    """hanime 的标签词表（含条目数），供前端渲染多选。

    这是 hanime 相对 tube 站最大的优势：标签是结构化元数据，能做精确 AND。
    """

    def _work() -> dict[str, int]:
        c = _hanime_http()
        hanime_mod.load_catalog(c)
        return hanime_mod.catalog_tags()

    try:
        counts = await run_in_threadpool(_work)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"hanime 标签不可用：{type(exc).__name__}: {exc}") from exc

    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return JSONResponse({
        "count": len(counts),
        "tags": [{"name": k, "count": v} for k, v in ordered],
    })


@app.get("/api/random")
async def api_random(
    site: str = Query("RedTube"),
    count: int = Query(10, ge=1, le=60),
) -> JSONResponse:
    """无关键词时的"随便看看"：随机返回 count 个可播放的视频。

    两个站形态不同，做法也不同：
      * tube 站（paged）—— 站点没有"给我随机视频"的接口，只能随机挑一个列表页
        （PornHub 还会随机页码），解析后从结果里随机抽 count 条
      * hanime（catalog）—— 全量目录本来就在内存里（约 3400 条），直接真随机抽

    返回结构和 /api/search 对齐（额外多个 random: true），前端可以复用 renderGrid。
    """
    adapter = get_site(site)
    if adapter is None:
        raise HTTPException(404, f"未知站点：{site}")

    # ---- catalog 类站点：内存里直接抽 ----
    if getattr(adapter, "search_kind", "paged") == "catalog":
        def _work() -> list[dict[str, Any]]:
            hanime_mod.load_catalog(_hanime_http())
            return hanime_mod.catalog_items()

        try:
            pool = await run_in_threadpool(_work)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"hanime 目录不可用：{type(exc).__name__}: {exc}") from exc
        return JSONResponse(_random_payload(adapter, _pick_random(pool, count, adapter.key)))

    # ---- tube 站：随机挑列表页 + 池内抽样 ----
    if not adapter.browse_urls():
        raise HTTPException(400, f"{adapter.name} 不支持无关键词浏览")

    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if adapter.cookie:
        headers["Cookie"] = adapter.cookie

    pool = await _browse_rows(adapter, headers)
    usable = [x for x in pool if _long_enough(x)]
    return JSONResponse(_random_payload(adapter, _pick_random(usable, count, adapter.key)))


async def _browse_rows(
    adapter: type[SiteAdapter], headers: dict[str, str]
) -> list[dict[str, Any]]:
    """抓一轮浏览页，并入"站点累积候选池"，返回候选池。

    为什么要累积：入口 URL 是猜的（随机分类页 / 随机页码），单次只能覆盖一小块。
    RedTube 六个入口合计才 225 条、任取两个合并只有 78 条，光从这一小撮里抽，
    连续点「刷新」平均会撞上 3 个；从累积池里抽就降到 1 个以下。
    同时每轮都真去抓新页，所以内容本身还是新鲜的。

    猜错入口很正常 —— PornHub 的页码上限本身就在浮动。所以最多猜两轮；
    同一轮里挂掉一个入口不算致命，剩下的还能用；全挂了但池子里还有货就凑合用。
    """
    fresh: list[dict[str, Any]] = []
    seen: set[str] = set()
    last_exc: Exception | None = None

    for _ in range(2):
        for url in adapter.browse_urls():
            rows = browse_cache.get(url)
            if rows is None:
                try:
                    r = await client.get(url, headers=headers)
                    r.raise_for_status()
                except httpx.HTTPError as exc:
                    last_exc = exc
                    continue
                rows, _tp, _tc = adapter.parse_search(r.text, 1)
                # 解析不出东西就别缓存 —— 否则这个入口 10 分钟内会一直返回空
                if rows:
                    browse_cache.set(url, rows)
            # 不同入口之间会互相推荐对方的视频，按 id 去重
            for x in rows:
                if x["id"] not in seen:
                    seen.add(x["id"])
                    fresh.append(x)
        if fresh:
            return _merge_browse_union(adapter, fresh)

    union = _browse_union(adapter)
    if union:
        return union                      # 新页全挂了，但旧池子还能用
    raise HTTPException(502, f"浏览页请求失败（{adapter.name}）：{upstream_error_detail(last_exc)}")


# 站点累积候选池最多留多少条，防止长跑时无限增长
BROWSE_UNION_MAX = 1200


def _union_key(adapter: type[SiteAdapter]) -> str:
    # 不是真 URL，只是借 browse_cache 的 TTL 机制（过期后重新攒）
    return f"@{adapter.key}"


def _browse_union(adapter: type[SiteAdapter]) -> list[dict[str, Any]]:
    return browse_cache.get(_union_key(adapter)) or []


def _merge_browse_union(
    adapter: type[SiteAdapter], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把刚抓到的条目并入累积池（新数据覆盖旧的，超限丢最老的）。"""
    merged: dict[str, dict[str, Any]] = {x["id"]: x for x in _browse_union(adapter)}
    merged.update({x["id"]: x for x in fresh})
    out = list(merged.values())
    if len(out) > BROWSE_UNION_MAX:
        out = out[-BROWSE_UNION_MAX:]
    browse_cache.set(_union_key(adapter), out)
    return out


def _random_payload(adapter: type[SiteAdapter], picks: list[dict[str, Any]]) -> dict[str, Any]:
    """把随机抽样包成和 /api/search 一样的结构。"""
    return {
        "site": adapter.key,
        "site_name": adapter.name,
        "query": "",
        "terms": [],
        "term_count": 0,
        "tags": [],
        "random": True,          # 前端据此隐藏分页条
        "page": 1,
        "total_pages": None,     # 随机推荐没有"下一页"的概念
        "total_count": "",
        "count": len(picks),
        "results": picks,
    }


def page_url_from_id(site: str, vid: str) -> str:
    """由站点 id 拼出视频页 URL。

    id 的形状**各站不同**（RedTube 纯数字、PornHub 是 viewkey、hanime 是 slug），
    所以只校验 URL 安全字符，具体怎么拼交给适配器的 video_page_url()。
    """
    adapter = get_site(site)
    if adapter is None:
        raise HTTPException(404, f"未知站点：{site}")
    if getattr(adapter, "search_kind", "paged") == "catalog":
        # hanime 的 slug 是字母数字连字符
        if not re.fullmatch(r"[A-Za-z0-9\-_]+", vid):
            raise HTTPException(400, "id 格式非法")
    elif not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", vid):
        raise HTTPException(400, "id 格式非法")
    return adapter.video_page_url(vid)


async def resolve_any(url: str, refresh: bool = False) -> dict[str, Any]:
    """resolve 一个视频页 URL，返回统一形状的 info。

    两条路：hanime 走自研的 WASM 签名握手，其余交给 yt-dlp。
    /api/resolve 和 /api/download 都用它，保证两边拿到的东西完全一致。
    """
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "url 必须是 http/https")
    # hanime 不走 yt-dlp：取流要过 WASM 签名的握手，见 hanime.py
    if hanime_mod.slug_from_url(url):
        return await run_in_threadpool(_resolve_hanime, url)
    return await resolve(url, refresh=refresh)


@app.get("/api/resolve")
async def api_resolve(
    url: str | None = Query(None, description="视频页面 URL"),
    id: str | None = Query(None, description="站点视频 ID（配合 site）"),
    site: str = Query("RedTube"),
    refresh: bool = Query(False),
) -> JSONResponse:
    if not url:
        if not id:
            raise HTTPException(400, "需要 url 或 id 参数")
        url = page_url_from_id(site, id)

    return JSONResponse(await resolve_any(url, refresh))


def _resolve_hanime(url: str) -> dict[str, Any]:
    """hanime 取流：握手 → sources[]。

    返回的结构刻意和 yt-dlp 那条路保持一致（title/formats/thumbnail…），
    这样前端只需要处理一种形状；唯一多出来的是 formats[].is_hls=True，
    前端据此决定要不要上 hls.js。
    """
    slug = hanime_mod.slug_from_url(url)
    if not slug:
        raise HTTPException(400, "不是有效的 hanime 视频页 URL")

    try:
        c = _hanime_http()
        catalog = hanime_mod.load_catalog(c)
        streams = hanime_mod.resolve_streams(c, _hanime_signer_instance(), slug)
    except hanime_mod.SignerError as exc:
        raise HTTPException(502, f"hanime 取流失败：{exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"hanime 取流网络错误：{upstream_error_detail(exc)}") from exc

    meta = catalog["by_slug"].get(slug) or {}
    if not streams:
        raise HTTPException(404, "hanime 没有返回可用清晰度"
                                 "（游客仅限 ≤720p）")

    formats = [{
        "height": s["height"],
        "label": s["label"],
        "url": s["url"],
        "filesize": None,
        "is_hls": True,          # ← 前端据此用 hls.js 而不是 <video src>
        "kind": s.get("kind") or "",
    } for s in streams]

    return {
        "id": slug,
        "title": meta.get("title") or slug,
        "duration": None,
        "uploader": meta.get("uploader") or "",
        "view_count": None,
        "upload_date": None,
        "thumbnail": meta.get("thumbnail") or "",
        "webpage_url": url,
        "description": "",
        "tags": meta.get("tags") or [],
        "formats": formats,
    }


_RE_OG_IMAGE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_RE_OG_IMAGE_REV = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)

# 兜底封面的缓存：og:image 基本不会变，可以缓存得久一点
thumb_cache = TTLCache(ttl=6 * 3600, maxsize=512)


@app.get("/api/thumb")
async def api_thumb(
    id: str = Query(..., description="视频 ID"),
    site: str = Query("RedTube"),
) -> JSONResponse:
    """兜底封面：直接去视频页取 og:image。

    列表页的封面走 CDN77，有一部分视频的所有尺寸都会稳定 502。
    视频页的 og:image 是另一条路径（``rs:fit:1280:720``），通常是好的。
    这个接口只在列表页所有候选都失败时才会被前端调用，不影响正常加载。
    """
    adapter = get_site(site)
    if adapter is None:
        raise HTTPException(404, f"未知站点：{site}")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", id):
        raise HTTPException(400, "id 格式非法")

    key = f"{site}:{id}"
    cached = thumb_cache.get(key)
    if cached is not None:
        return JSONResponse({"url": cached})

    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if adapter.cookie:
        headers["Cookie"] = adapter.cookie

    try:
        r = await client.get(adapter.video_page_url(id), headers=headers)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"取视频页失败：{upstream_error_detail(exc)}") from exc

    m = _RE_OG_IMAGE.search(r.text) or _RE_OG_IMAGE_REV.search(r.text)
    url = html_mod.unescape(m.group(1)).strip() if m else ""
    if not url:
        raise HTTPException(404, "视频页里没有 og:image")

    thumb_cache.set(key, url)
    return JSONResponse({"url": url})


# 连接类异常：这些才值得换路由/重试；HTTP 状态码类的不算（send 也不会为状态码抛异常）
_UPSTREAM_CONNECT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)


async def _open_upstream(
    url: str, headers: dict[str, str]
) -> tuple[httpx.Response, httpx.AsyncClient | None]:
    """打开上游连接，返回 (响应, 需要额外关闭的一次性客户端或 None)。

    为什么要这么绕：实测两条路**都会偶发失败**，而且原因不同 ——

    * **代理**：hanime 有部分分片 CDN 挂在 Cloudflare 后面，代理节点在 TLS 层
      被直接拒掉（``SSL: UNEXPECTED_EOF_WHILE_READING``），而住宅直连正常。
    * **直连**：同一个主机也会偶发 TLS 握手被重置（Cloudflare 边缘抖动）。
      实测同一 URL 连发 3 次：2 次 200、1 次 ConnectError，底层是 anyio 的
      ``BrokenResourceError``。这跟请求头无关（带不带 Range 都会），纯网络抖动。

    反过来 hanime.tv 主站在国内被墙、**必须**走代理。所以顺序是
    「代理 → 直连 → 代理 → 直连」，每轮之间等一下，兜住抖动。
    """
    attempts = 2 if PROXY else 3
    last: Exception | None = None

    for i in range(attempts):
        if i:
            await asyncio.sleep(0.35 * i)

        # 1) 走代理（复用长连接池）
        try:
            return await client.send(
                client.build_request("GET", url, headers=headers), stream=True
            ), None
        except _UPSTREAM_CONNECT_ERRORS as exc:
            last = exc
        except httpx.HTTPError as exc:
            last = exc

        if not PROXY:
            continue

        # 2) 直连重试。刻意用**一次性客户端**：实测在复用的 AsyncClient 上做
        #    send(stream=True) 有时会直接抛 ConnectError，换成新建的就正常。
        direct = _make_client(use_proxy=False)
        try:
            return await direct.send(
                direct.build_request("GET", url, headers=headers), stream=True
            ), direct
        except httpx.HTTPError as exc:
            await direct.aclose()
            last = exc

    raise HTTPException(
        502,
        f"上游连不上（代理与直连都试过了）："
        f"{upstream_error_detail(last) if last else '未知错误'}",
    )


@app.get("/api/media")
async def api_media(
    request: Request,
    u: str = Query(..., description="上游媒体 URL"),
    r: str | None = Query(None, description="要转发给 CDN 的 Referer（视频页 URL）"),
    dl: str = Query("", description="传文件名则作为附件下载（Content-Disposition）"),
):
    """Range-aware 流式中转。视频、缩略图都走这里。"""
    if not re.match(r"^https?://", u):
        raise HTTPException(400, "非法 URL")
    if not host_allowed(u):
        raise HTTPException(403, f"域名不在白名单内：{urlparse(u).hostname}")

    fwd: dict[str, str] = {"User-Agent": UA}
    if rng := request.headers.get("range"):
        fwd["Range"] = rng
    # <video src> 没法自定义请求头，所以 Referer 用 query 参数传进来；
    # 没传就按 CDN 域名兜一个默认值（图片 CDN 不带会被 403）。
    referer = r or request.headers.get("x-adskiper-referer") or default_referer_for(u)
    if referer and re.match(r"^https?://", referer):
        fwd["Referer"] = referer

    upstream, fallback_client = await _open_upstream(u, fwd)

    ctype = upstream.headers.get("content-type", "")

    async def _cleanup() -> None:
        """关掉上游响应；如果这次走了直连兜底，把那个一次性客户端也关掉。"""
        try:
            await upstream.aclose()
        finally:
            if fallback_client is not None:
                await fallback_client.aclose()

    # HLS 播放列表要特殊处理：里面的分片/密钥地址是**绝对 URL**，如果原样交给
    # 浏览器，hls.js 会绕过我们的代理去直连那些（被墙的）CDN。所以读进来把每条
    # URI 改写成再走一次本代理。播放列表很小（~10KB），整体读出没有性能问题。
    if _is_hls_playlist(u, ctype):
        try:
            raw = await upstream.aread()
        finally:
            await _cleanup()
        text = _rewrite_m3u8(raw.decode("utf-8", "replace"), u)
        return Response(
            content=text.encode("utf-8"),
            media_type="application/vnd.apple.mpegurl",
            headers={"cache-control": "no-store", "access-control-allow-origin": "*"},
        )

    passthrough = ("content-type", "content-length", "content-range",
                   "accept-ranges", "content-encoding", "last-modified", "etag")
    out: dict[str, str] = {}
    for k in passthrough:
        if v := upstream.headers.get(k):
            out[k] = v

    out.setdefault("accept-ranges", "bytes")
    # 「另存为」：tube 站是单个 MP4，走这里直接带上附件头即可，无需 ffmpeg
    if dl:
        out["content-disposition"] = attachment_header(dl)
    # 图片可以长缓存，视频不缓存（签名 URL 会过期）
    if ctype.startswith("image/"):
        out["cache-control"] = "public, max-age=86400"
    else:
        out["cache-control"] = "no-store"

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except Exception:
            # 客户端中途断开（切清晰度 / 关页面 / 拖进度条）时会抛连接类异常，
            # 这是流式代理的正常情况，静默即可。
            pass

    # 用 BackgroundTask 关闭上游连接：比在生成器 finally 里 aclose() 更可靠，
    # 因为任务被取消时 finally 里的 await 可能不会执行完，导致连接池泄漏。
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=out,
        background=BackgroundTask(_cleanup),
    )


# --------------------------------------------------------------------------- #
# 「另存为」
# --------------------------------------------------------------------------- #

def safe_filename(title: str, ext: str) -> str:
    """把视频标题变成一个安全的文件名。中文保留，只去掉路径分隔符和控制字符。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    # 有的站点标题自带扩展名，别拼成 "xxx.mp4.mp4"
    name = re.sub(r"\.(mp4|mkv|webm|avi|mov|ts|m4v)$", "", name, flags=re.I)
    if len(name) > 120:
        name = name[:120].rstrip(" .")
    return f"{name or 'video'}.{ext}"


def attachment_header(filename: str) -> str:
    """构造 Content-Disposition。

    中文标题必须同时给两套：老式 ``filename=`` 是 ASCII 兜底（非 ASCII 会被丢掉），
    RFC 5987 的 ``filename*=UTF-8''`` 才是现代浏览器真正用的那个 —— 只写前者的话
    中文标题会存成乱码或直接变成 "video"。
    """
    stem, _, ext = filename.rpartition(".")
    ascii_stem = stem.encode("ascii", "ignore").decode().strip(" ._")
    # 纯中文/日文标题会被 ascii 过滤得只剩零星字符（甚至只剩个数字），这时退回 video
    if len(ascii_stem) < 2:
        ascii_stem = "video"
    ascii_name = f"{ascii_stem}.{ext}" if ext else ascii_stem
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def _download_hls(m3u8: str, info: dict[str, Any], page_url: str) -> Response:
    """用 ffmpeg 把 HLS 边解密边封装成 MP4，**流式**回给浏览器（不落盘）。

    为什么必须流式：一部番剧实测 142 个分片、约 294 MB。先落盘的话要么占 300MB
    磁盘，要么让用户对着一个没有任何反馈的按钮等半分钟。
    """
    if not FFMPEG:
        raise HTTPException(
            503,
            "没有找到 ffmpeg。hanime 是 HLS（一部番剧上百个 AES-128 加密分片），"
            "「另存为」需要 ffmpeg 来解密和封装；也可以用 ADSKIPER_FFMPEG "
            "指定它的绝对路径。RedTube / PornHub 是单个 MP4，不需要 ffmpeg。",
        )

    # 关键：让 ffmpeg 读**我们自己代理改写过的**播放列表。这样分片和 AES 密钥都经
    # /api/media 转发，于是 ffmpeg 自己既不用配代理、也不受域名白名单影响。
    playlist = f"http://127.0.0.1:{PORT}/api/media?" + urlencode({"u": m3u8, "r": page_url})

    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-nostdin",              # 别让 ffmpeg 去抢我们的 stdin
        "-i", playlist,
        "-c", "copy",
        # HLS 分片里的 AAC 带 ADTS 头，塞进 MP4 必须剥掉。写普通文件时 ffmpeg 会
        # **自动补**这个滤镜，输出到管道时不会 —— 实测不加会报 "Malformed AAC
        # bitstream detected" 并且只产出 2KB 垃圾。
        "-bsf:a", "aac_adtstoasc",
        # 管道不可 seek，"+faststart" 会直接报 "muxer does not support non seekable
        # output"。改用 fragmented MP4：moov 写在最前面（实测偏移 32），能边生成边发。
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    # stderr 必须有人一直读：管道写满会把 ffmpeg 卡死，而且失败时要靠它给出原因
    errbuf: list[bytes] = []

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            b = await proc.stderr.read(4096)
            if not b:
                break
            errbuf.append(b)

    err_task = asyncio.create_task(_drain_stderr())

    # 先等第一块数据再决定返回什么：ffmpeg 一上来就失败（URL 失效、站点改版）时，
    # 这样还来得及返回一个正常的 JSON 错误，而不是让浏览器下到一个 0 字节的坏文件。
    assert proc.stdout is not None
    try:
        first = await asyncio.wait_for(proc.stdout.read(65536), timeout=45)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "ffmpeg 45 秒内没有产出任何数据，可能上游播放列表有问题")

    if not first:
        await proc.wait()
        err_task.cancel()
        detail = b"".join(errbuf).decode("utf-8", "replace").strip()
        raise HTTPException(502, f"ffmpeg 没能产出视频：{detail[:300] or '未知错误'}")

    name = safe_filename(info.get("title") or "", "mp4")

    async def _cleanup() -> None:
        """客户端下完 / 中途取消时收尾。用 BackgroundTask 保证一定跑到。"""
        if proc.returncode is None:
            proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        err_task.cancel()

    async def body():
        try:
            yield first
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        except Exception:  # noqa: BLE001
            # 客户端中途取消下载是正常情况，静默即可
            pass

    return StreamingResponse(
        body(),
        media_type="video/mp4",
        headers={
            "content-disposition": attachment_header(name),
            "cache-control": "no-store",
        },
        background=BackgroundTask(_cleanup),
    )


@app.get("/api/download")
async def api_download(
    url: str | None = Query(None, description="视频页面 URL"),
    id: str | None = Query(None, description="站点视频 ID（配合 site）"),
    site: str = Query("RedTube"),
    quality: str = Query("", description="清晰度 label（如 720p）；留空 = 最高"),
) -> Response:
    """「另存为」一个视频。两种形态走两条路：

    * **tube 站** —— 单个渐进式 MP4。307 跳到 /api/media 并带上附件头，
      复用那边已经写好的 Range / Referer / 域名白名单逻辑，一行都不用重复。
    * **hanime** —— HLS，交给 ffmpeg（见 _download_hls）。
    """
    if not url:
        if not id:
            raise HTTPException(400, "需要 url 或 id 参数")
        url = page_url_from_id(site, id)

    info = await resolve_any(url)
    formats = info.get("formats") or []
    if not formats:
        raise HTTPException(502, "这个视频没有可下载的清晰度")

    # 选清晰度：按 label 精确匹配；匹配不上就用第一个
    # （yt-dlp 那条路已按高度降序，hanime 那条也是 720p 在前，所以第一个即最高）
    fmt = next((f for f in formats if f.get("label") == quality), formats[0])

    if fmt.get("is_hls"):
        return await _download_hls(fmt["url"], info, url)

    name = safe_filename(info.get("title") or "", "mp4")
    return RedirectResponse(
        "/api/media?" + urlencode({"u": fmt["url"], "r": url, "dl": name}),
        status_code=307,
    )


# --------------------------------------------------------------------------- #
# HLS 播放列表改写
# --------------------------------------------------------------------------- #

def _is_hls_playlist(url: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if "mpegurl" in ct or "vnd.apple" in ct:
        return True
    path = urlparse(url).path.lower()
    return path.endswith(".m3u8")


_RE_M3U8_URI_ATTR = re.compile(r'URI="([^"]+)"')


def _proxy_url(target: str) -> str:
    return "/api/media?u=" + quote(target, safe="")


def _rewrite_m3u8(text: str, base_url: str) -> str:
    """把播放列表里的 URI 全部改写成经本代理。

    需要处理两种：
      * 裸行（分片或子播放列表地址）
      * 标签属性里的 URI="…"（典型是 #EXT-X-KEY 的 AES 密钥、#EXT-X-MAP 的初始化段）

    相对地址按 ``base_url`` 解析成绝对地址后再包一层。
    """
    out_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            out_lines.append(raw_line)
            continue

        if line.startswith("#"):
            def _sub(m: re.Match[str]) -> str:
                absolute = urljoin(base_url, m.group(1))
                return f'URI="{_proxy_url(absolute)}"'

            out_lines.append(_RE_M3U8_URI_ATTR.sub(_sub, raw_line))
            continue

        out_lines.append(_proxy_url(urljoin(base_url, line)))

    # 播放列表必须以换行结尾
    return "\n".join(out_lines) + "\n"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> None:
    import uvicorn

    print("=" * 68)
    print("  NoADPornSite")
    print(f"  前端      http://{HOST}:{PORT}")
    print(f"  出口代理  {PROXY or '(直连)'}")
    print(f"  媒体白名单 共 {len(MEDIA_SUFFIXES)} 个域名后缀")
    print("=" * 68)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
