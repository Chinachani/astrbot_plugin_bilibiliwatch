# -*- coding: utf-8 -*-
import asyncio
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video
from astrbot.api.star import Context, Star, register


API_VIEW = "https://api.bilibili.com/x/web-interface/view"

BV_REGEX = re.compile(r"\b(BV[0-9A-Za-z]{10})\b", re.IGNORECASE)
AV_REGEX = re.compile(r"\bav(\d+)\b", re.IGNORECASE)
VIDEO_URL_REGEX = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/([A-Za-z0-9]+)")
SHORT_URL_REGEX = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")
APP_SCHEME_REGEX = re.compile(r"bilibili://video/(\d+)")
BANGUMI_EP_REGEX = re.compile(r"https?://(?:www\.)?bilibili\.com/bangumi/play/ep(\d+)", re.IGNORECASE)
BANGUMI_SS_REGEX = re.compile(r"https?://(?:www\.)?bilibili\.com/bangumi/play/ss(\d+)", re.IGNORECASE)
EP_ID_REGEX = re.compile(r"\bep(\d+)\b", re.IGNORECASE)
SS_ID_REGEX = re.compile(r"\bss(\d+)\b", re.IGNORECASE)


@dataclass
class VideoTarget:
    bvid: Optional[str] = None
    aid: Optional[str] = None
    ep_id: Optional[str] = None
    ss_id: Optional[str] = None
    source: str = ""


@register(
    "astrbot_plugin_bilibiliwatch",
    "Chinachani",
    "B站链接解析与视频信息展示插件",
    "1.0.0",
    "https://github.com/Chinachani/astrbot_plugin_bilibiliwatch",
)
class BilibiliWatch(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_json_payload: Optional[str] = None
        self._callback_tasks: Dict[str, Dict[str, Any]] = {}
        self._callback_server: Optional[ThreadingHTTPServer] = None
        self._callback_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_event_text(self, event: AstrMessageEvent) -> str:
        candidates = [
            "message_str",
            "plain_text",
            "raw_message",
            "message",
            "text",
            "message_chain",
        ]
        for name in candidates:
            val = getattr(event, name, None)
            if val is None:
                continue
            if callable(val):
                try:
                    val = val()
                except Exception:
                    continue
            for meth in ("get_plain_text", "to_plain_text"):
                func = getattr(val, meth, None)
                if callable(func):
                    try:
                        text = func()
                    except Exception:
                        text = ""
                    if isinstance(text, str) and text.strip():
                        return text.strip()
            if isinstance(val, str) and val.strip():
                return val.strip()
            try:
                val_str = str(val).strip()
            except Exception:
                val_str = ""
            if val_str:
                return val_str
        return ""

    def _extract_args_from_event(self, event: AstrMessageEvent, cmd_name: str) -> str:
        text = self._get_event_text(event)
        if not text:
            return ""
        lowered = text.strip()
        if lowered.startswith("/"):
            lowered = lowered[1:]
        if lowered.lower().startswith(cmd_name.lower()):
            return text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        return ""

    def _cookie_header(self) -> str:
        return str(self.config.get("bilibili_cookie", "") or "").strip()

    def _user_agent(self) -> str:
        return str(self.config.get("user_agent", "") or "").strip()

    def _timeout(self) -> int:
        try:
            return int(self.config.get("api_timeout_sec", 10))
        except Exception:
            return 10

    def _retries(self) -> int:
        try:
            return int(self.config.get("request_retries", 2))
        except Exception:
            return 2

    def _retry_delay(self) -> float:
        try:
            return float(self.config.get("retry_delay_sec", 1))
        except Exception:
            return 1

    def _debug(self) -> bool:
        return bool(self.config.get("debug_log", False))

    def _enable_parse_hint(self) -> bool:
        return bool(self.config.get("enable_parse_hint", True))

    def _enable_http_callback(self) -> bool:
        return bool(self.config.get("enable_http_callback", False))

    def _callback_host(self) -> str:
        return str(self.config.get("callback_listen_host", "0.0.0.0") or "0.0.0.0").strip()

    def _callback_port(self) -> int:
        try:
            return int(self.config.get("callback_listen_port", 8787))
        except Exception:
            return 8787

    def _callback_token(self) -> str:
        return str(self.config.get("callback_token", "") or "").strip()

    def _callback_fallback_sec(self) -> int:
        try:
            return int(self.config.get("callback_fallback_sec", 15))
        except Exception:
            return 15

    def _callback_path(self) -> str:
        return "/bili/callback"

    def _set_config_value(self, key: str, value: Any) -> bool:
        try:
            setter = getattr(self.config, "set", None)
            if callable(setter):
                setter(key, value)
                return True
        except Exception:
            pass
        try:
            updater = getattr(self.config, "update", None)
            if callable(updater):
                updater({key: value})
                return True
        except Exception:
            pass
        try:
            self.config[key] = value
            return True
        except Exception:
            pass
        return False

    async def _set_remote_callback(self, base_url: str, callback_url: str, callback_token: str) -> Tuple[bool, str]:
        params = {
            "token": self._api_token(),
            "callback_url": callback_url,
            "callback_token": callback_token,
        }
        status, data = await self._request_json_method("POST", f"{base_url}/api/callback/config", params)
        if status != 200 or not isinstance(data, dict) or not data.get("success"):
            return False, f"回调配置失败：HTTP {status}"
        return True, "回调配置已更新。"

    def _page_size(self) -> int:
        return 10

    def _paginate(self, items: list, page: int) -> Tuple[int, int, list]:
        if page < 1:
            page = 1
        total = len(items)
        if total == 0:
            return page, 1, []
        page_size = self._page_size()
        total_pages = (total + page_size - 1) // page_size
        if page > total_pages:
            page = total_pages
        start = (page - 1) * page_size
        end = start + page_size
        return page, total_pages, items[start:end]

    def _parse_page(self, value: str) -> int:
        try:
            page = int(value)
        except Exception:
            return 1
        return page if page > 0 else 1

    def _show_cover(self) -> bool:
        return bool(self.config.get("show_cover", True))

    def _enable_video_output(self) -> bool:
        return bool(self.config.get("enable_video_output", False))

    def _enable_auto(self) -> bool:
        return bool(self.config.get("enable_auto_detect", True))

    def _enable_parse_miniapp(self) -> bool:
        return bool(self.config.get("enable_parse_miniapp", True))

    def _api_base(self) -> str:
        return str(self.config.get("video_api_base_url", "") or "").strip().rstrip("/")

    def _api_token(self) -> str:
        return str(self.config.get("video_api_login_token", "") or "").strip()

    async def _request_json(self, url: str, params: Dict[str, Any]) -> Tuple[int, Any]:
        headers = {}
        ua = self._user_agent()
        if ua:
            headers["User-Agent"] = ua
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        timeout = httpx.Timeout(self._timeout())
        last_exc = None
        for attempt in range(self._retries() + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    resp = await client.get(url, params=params, headers=headers)
                return resp.status_code, resp.json()
            except Exception as exc:
                last_exc = exc
                if self._debug():
                    logger.error("bili: request failed attempt %s: %s", attempt + 1, exc)
                if attempt < self._retries():
                    await asyncio.sleep(self._retry_delay())
        return 599, {"error": str(last_exc)}

    async def _request_json_method(self, method: str, url: str, params: Dict[str, Any]) -> Tuple[int, Any]:
        headers = {}
        ua = self._user_agent()
        if ua:
            headers["User-Agent"] = ua
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        timeout = httpx.Timeout(self._timeout())
        last_exc = None
        for attempt in range(self._retries() + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    resp = await client.request(method.upper(), url, params=params, headers=headers)
                return resp.status_code, resp.json()
            except Exception as exc:
                last_exc = exc
                if self._debug():
                    logger.error("bili: request failed attempt %s: %s", attempt + 1, exc)
                if attempt < self._retries():
                    await asyncio.sleep(self._retry_delay())
        return 599, {"error": str(last_exc)}

    async def _request_text(self, url: str, params: Dict[str, Any]) -> Tuple[int, str]:
        headers = {}
        ua = self._user_agent()
        if ua:
            headers["User-Agent"] = ua
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        timeout = httpx.Timeout(self._timeout())
        last_exc = None
        for attempt in range(self._retries() + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    resp = await client.get(url, params=params, headers=headers)
                return resp.status_code, resp.text
            except Exception as exc:
                last_exc = exc
                if self._debug():
                    logger.error("bili: request failed attempt %s: %s", attempt + 1, exc)
                if attempt < self._retries():
                    await asyncio.sleep(self._retry_delay())
        return 599, str(last_exc or "")

    async def _resolve_short_url(self, url: str) -> str:
        headers = {}
        ua = self._user_agent()
        if ua:
            headers["User-Agent"] = ua
        timeout = httpx.Timeout(self._timeout())
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                return str(resp.url)
        except Exception as exc:
            if self._debug():
                logger.error("bili: resolve short link failed: %s", exc)
            return ""

    def _find_target_in_text(self, text: str) -> Optional[VideoTarget]:
        if not text:
            return None

        # Bangumi full URL
        m = BANGUMI_EP_REGEX.search(text)
        if m:
            return VideoTarget(ep_id=m.group(1), source=m.group(0))
        m = BANGUMI_SS_REGEX.search(text)
        if m:
            return VideoTarget(ss_id=m.group(1), source=m.group(0))

        # Full video URL
        m = VIDEO_URL_REGEX.search(text)
        if m:
            raw = m.group(1)
            if raw.lower().startswith("av"):
                aid = re.sub(r"[^0-9]", "", raw)
                return VideoTarget(aid=aid, source=raw)
            return VideoTarget(bvid=self._normalize_bvid(raw), source=raw)

        # Short URL
        m = SHORT_URL_REGEX.search(text)
        if m:
            return VideoTarget(source=m.group(0))

        # BV
        m = BV_REGEX.search(text)
        if m:
            return VideoTarget(bvid=self._normalize_bvid(m.group(1)), source=m.group(1))

        # AV
        m = AV_REGEX.search(text)
        if m:
            return VideoTarget(aid=m.group(1), source=m.group(0))

        # EP / SS
        m = EP_ID_REGEX.search(text)
        if m:
            return VideoTarget(ep_id=m.group(1), source=m.group(0))
        m = SS_ID_REGEX.search(text)
        if m:
            return VideoTarget(ss_id=m.group(1), source=m.group(0))

        # App scheme
        m = APP_SCHEME_REGEX.search(text)
        if m:
            return VideoTarget(aid=m.group(1), source=m.group(0))

        return None
    
    def _normalize_bvid(self, bvid: str) -> str:
        if not bvid:
            return bvid
        if bvid[:2].lower() == "bv":
            return "BV" + bvid[2:]
        return bvid

    async def _resolve_target(self, target: VideoTarget) -> Optional[VideoTarget]:
        if target.bvid or target.aid or target.ep_id or target.ss_id:
            return target
        if target.source and "b23.tv" in target.source:
            resolved = await self._resolve_short_url(target.source)
            if resolved:
                return self._find_target_in_text(resolved)
        return None

    async def _fetch_video_info(self, target: VideoTarget) -> Tuple[bool, str, str, str, int, str]:
        if target.ep_id or target.ss_id:
            return await self._fetch_bangumi_info(target)
        params: Dict[str, Any] = {}
        if target.bvid:
            params["bvid"] = target.bvid
        elif target.aid:
            params["aid"] = target.aid
        else:
            return False, "无法识别视频 ID。", "", "", 0, ""

        if bool(self.config.get("prefer_official_api", False)):
            status, data = await self._request_json(API_VIEW, params)
            if status == 200 and isinstance(data, dict) and data.get("code") == 0:
                info = data.get("data", {})
                text, cover, link, duration_sec = self._format_video_info(info)
                return True, text, cover, link, duration_sec, info.get("title", "")

        custom_base = str(self.config.get("video_api_base_url", "") or "").strip().rstrip("/")
        if custom_base:
            ok, text, cover, link, duration_sec = await self._fetch_video_info_custom(custom_base, target)
            if ok:
                return True, text, cover, link, duration_sec, ""
            if bool(self.config.get("custom_api_fallback_official", True)):
                status, data = await self._request_json(API_VIEW, params)
                if status != 200:
                    return False, f"请求失败：HTTP {status}", "", "", 0
                if not isinstance(data, dict):
                    return False, "返回数据格式异常。", "", "", 0
                if data.get("code") != 0:
                    return False, f"API 错误：{data.get('message', '未知错误')}", "", "", 0
                info = data.get("data", {})
                text, cover, link, duration_sec = self._format_video_info(info)
                return True, text, cover, link, duration_sec
            return False, text, cover, link, duration_sec, ""

        status, data = await self._request_json(API_VIEW, params)
        if status != 200:
            return False, f"请求失败：HTTP {status}", "", "", 0, ""
        if not isinstance(data, dict):
            return False, "返回数据格式异常。", "", "", 0, ""
        if data.get("code") != 0:
            return False, f"API 错误：{data.get('message', '未知错误')}", "", "", 0, ""

        info = data.get("data", {})
        text, cover, link, duration_sec = self._format_video_info(info)
        return True, text, cover, link, duration_sec, info.get("title", "")

    async def _fetch_bangumi_info(self, target: VideoTarget) -> Tuple[bool, str, str, str, int, str]:
        params: Dict[str, Any] = {}
        if target.ep_id:
            params["ep_id"] = target.ep_id
        elif target.ss_id:
            params["season_id"] = target.ss_id
        else:
            return False, "无法识别番剧 ID。", "", "", 0, ""

        url = "https://api.bilibili.com/pgc/view/web/season"
        status, data = await self._request_json(url, params)
        if status != 200:
            return False, f"番剧请求失败：HTTP {status}", "", "", 0, ""
        if not isinstance(data, dict):
            return False, "番剧返回数据格式异常。", "", "", 0, ""
        if data.get("code") != 0:
            return False, f"番剧 API 错误：{data.get('message', '未知错误')}", "", "", 0, ""

        result = data.get("result", {}) or {}
        season_title = result.get("season_title") or result.get("title") or "(无标题)"
        cover = result.get("cover") or ""
        stat = result.get("stat", {}) or {}
        view = self._format_num(stat.get("views") or stat.get("view"))
        danmaku = self._format_num(stat.get("danmakus") or stat.get("danmaku"))
        reply = self._format_num(stat.get("reply"))
        favorite = self._format_num(stat.get("favorites") or stat.get("favorite"))
        like = self._format_num(stat.get("likes") or stat.get("like"))
        evaluate = (result.get("evaluate") or "").strip()

        ep_title = ""
        duration_sec = 0
        episodes = result.get("episodes", []) or []
        if target.ep_id and episodes:
            for ep in episodes:
                if str(ep.get("id")) == str(target.ep_id):
                    short_title = str(ep.get("title") or "").strip()
                    long_title = str(ep.get("long_title") or "").strip()
                    share_copy = str(ep.get("share_copy") or "").strip()
                    if share_copy:
                        ep_title = share_copy
                    elif short_title and long_title:
                        ep_title = f"第{short_title}话 {long_title}"
                    elif short_title:
                        ep_title = f"第{short_title}话"
                    else:
                        ep_title = long_title
                    try:
                        duration_sec = int(ep.get("duration") or 0)
                    except Exception:
                        duration_sec = 0
                    if ep.get("cover") and not cover:
                        cover = ep.get("cover")
                    break

        link = ""
        if target.ep_id:
            link = f"https://www.bilibili.com/bangumi/play/ep{target.ep_id}"
        elif target.ss_id:
            link = f"https://www.bilibili.com/bangumi/play/ss{target.ss_id}"

        lines = [
            f"标题：{season_title}",
        ]
        if ep_title:
            lines.append(f"分集：{ep_title}")
        lines.extend(
            [
                f"播放：{view} | 弹幕：{danmaku} | 评论：{reply}",
                f"收藏：{favorite} | 点赞：{like}",
            ]
        )
        if evaluate:
            lines.append(f"简介：{self._shorten(evaluate, 80)}")
        if link:
            lines.append(f"番剧链接：{link}")
        return True, "\n".join(lines), cover, link, duration_sec, season_title

    async def _fetch_video_info_custom(self, base_url: str, target: VideoTarget) -> Tuple[bool, str, str, str, int, str]:
        link = self._make_video_url(target)
        if not link:
            return False, "无法生成视频链接。", "", "", 0, ""
        url = f"{base_url}/api/video/info"
        status, text = await self._request_text(url, {"url": link})
        if status != 200:
            return False, f"自定义 API 请求失败：HTTP {status}", "", "", 0, ""
        parsed = self._parse_custom_text(text)
        if not parsed:
            return False, "自定义 API 返回解析失败。", "", "", 0, ""
        return True, parsed["text"], parsed.get("cover", ""), parsed.get("link", ""), parsed.get("duration_sec", 0), parsed.get("title", "")

    def _format_video_info(self, info: Dict[str, Any]) -> Tuple[str, str, str, int]:
        title = info.get("title") or "(无标题)"
        bvid = info.get("bvid") or ""
        aid = info.get("aid") or ""
        owner = info.get("owner", {}) or {}
        up_name = owner.get("name") or "未知"
        up_mid = owner.get("mid") or ""
        desc = (info.get("desc") or "").strip()
        stat = info.get("stat", {}) or {}
        view = self._format_num(stat.get("view"))
        danmaku = self._format_num(stat.get("danmaku"))
        reply = self._format_num(stat.get("reply"))
        favorite = self._format_num(stat.get("favorite"))
        like = self._format_num(stat.get("like"))
        duration_raw = info.get("duration")
        duration = self._format_duration(duration_raw)
        duration_sec = self._parse_duration_to_seconds(duration_raw)
        pic = info.get("pic") or ""
        link = self._make_video_url(VideoTarget(bvid=bvid or None, aid=str(aid) if aid else None))

        lines = [
            f"标题：{title}",
            f"UP主：{up_name} ({up_mid})" if up_mid else f"UP主：{up_name}",
            f"时长：{duration}",
            f"播放：{view} | 弹幕：{danmaku} | 评论：{reply}",
            f"收藏：{favorite} | 点赞：{like}",
        ]
        if desc:
            lines.append(f"简介：{self._shorten(desc, 80)}")
        if link:
            lines.append(f"视频链接：{link}")
        # 主消息不再展示封面和视频链接，统一放入转发消息
        return "\n".join(lines), pic, link, duration_sec

    async def _send_forward_assets(self, event: AstrMessageEvent, cover: str, link: str):
        nodes: list[Node] = []
        if cover and self._show_cover():
            nodes.append(
                Node(
                    name="BiliWatch",
                    uin="0",
                    content=[Image.fromURL(cover)],
                )
            )
        if link and self._enable_video_output():
            video_url = await self._get_video_file_url(link)
            if video_url:
                nodes.append(
                    Node(
                        name="BiliWatch",
                        uin="0",
                        content=[Video.fromURL(video_url)],
                    )
                )
        if not nodes:
            return
        chain = MessageChain()
        chain.chain.append(Nodes(nodes))
        await event.send(chain)

    def _shorten(self, text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _format_duration(self, seconds: Any) -> str:
        try:
            sec = int(seconds)
        except Exception:
            return "未知"
        if sec <= 0:
            return "0:00"
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _format_num(self, value: Any) -> str:
        try:
            num = int(value)
        except Exception:
            return "0"
        if num >= 100_000_000:
            return f"{num / 100_000_000:.1f}亿"
        if num >= 10_000:
            return f"{num / 10_000:.1f}万"
        return str(num)

    def _looks_like_command(self, text: str) -> bool:
        if not text:
            return False
        text = text.strip()
        return text.startswith("/bili") or text.startswith("/哔哩")

    async def _handle_text(self, event: AstrMessageEvent, text: str):
        self._ensure_callback_server()
        target = self._find_target_in_text(text)
        if not target:
            return event.plain_result("没找到B站链接哦，小笨蛋。")
        resolved = await self._resolve_target(target)
        if not resolved:
            return event.plain_result("链接都解析不了，发个正确的啦。")
        if self._enable_parse_hint():
            await event.send(MessageChain().message("在解析呢，别催～"))
        ok, message, cover, link, duration_sec, title = await self._fetch_video_info(resolved)
        if not ok:
            return event.plain_result(f"拿不到信息呢：{message}")
        # 图文消息：封面在最上方
        chain = MessageChain()
        if cover and self._show_cover():
            chain.url_image(cover)
        chain.message(message)
        await event.send(chain)
        # 如需视频文件，转发消息里携带
        if self._enable_video_output() and link:
            max_len = self._max_video_duration_sec()
            if max_len > 0 and duration_sec > max_len:
                await event.send(MessageChain().message("视频太长啦，自己点链接去看嘛。"))
                return None
            await event.send(MessageChain().message("在抓视频啦，别催～"))
            # 转发消息：图文 + 视频
            asyncio.create_task(self._send_forward_bundle(event, message, cover, link, title, duration_sec))
        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def bili_parse_miniapp(self, event: AstrMessageEvent):
        if not self._enable_parse_miniapp():
            return
        msg_obj = getattr(event, "message_obj", None)
        msg_list = getattr(msg_obj, "message", None)
        if not msg_list:
            return
        for msg_element in msg_list:
            elem_type = getattr(msg_element, "type", None)
            if not elem_type:
                continue
            type_name = ""
            try:
                if hasattr(elem_type, "name"):
                    type_name = str(elem_type.name)
                else:
                    type_name = str(elem_type)
            except Exception:
                type_name = ""
            if "json" not in type_name.lower():
                continue

            json_payload = None
            for attr in ("data", "json", "raw", "content", "value"):
                if hasattr(msg_element, attr):
                    json_payload = getattr(msg_element, attr)
                    if json_payload is not None:
                        break
            if json_payload is None:
                continue
            try:
                self._last_json_payload = (
                    json.dumps(json_payload, ensure_ascii=False)
                    if isinstance(json_payload, (dict, list))
                    else str(json_payload)
                )
            except Exception:
                self._last_json_payload = str(json_payload)
            try:
                parsed_data = json_payload if isinstance(json_payload, dict) else json.loads(str(json_payload))
            except json.JSONDecodeError:
                if self._debug():
                    logger.error("bili: miniapp json decode failed: %s", json_payload)
                continue
            except Exception as exc:
                if self._debug():
                    logger.error("bili: miniapp json parse error: %s", exc)
                continue

            meta = parsed_data.get("meta", {}) if isinstance(parsed_data, dict) else {}
            detail_1 = meta.get("detail_1", {}) if isinstance(meta, dict) else {}
            title = detail_1.get("title")
            qqdocurl = detail_1.get("qqdocurl")
            detail_url = detail_1.get("url")

            if (title and "哔哩哔哩" in str(title)) and (qqdocurl or detail_url):
                link = str(qqdocurl or detail_url)
                if "https://b23.tv" in link:
                    resolved = await self._resolve_short_url(link)
                    if resolved:
                        link = resolved
                await self._handle_text(event, link)
                return

            news = meta.get("news", {}) if isinstance(meta, dict) else {}
            tag = news.get("tag", "")
            jumpurl = news.get("jumpUrl", "")
            if tag == "哔哩哔哩" and jumpurl:
                link = str(jumpurl)
                if "https://b23.tv" in link:
                    resolved = await self._resolve_short_url(link)
                    if resolved:
                        link = resolved
                await self._handle_text(event, link)
                return
            # fallback: scan payload text for bilibili links
            payload_text = self._last_json_payload or ""
            target = self._find_target_in_text(payload_text)
            if target:
                resolved = await self._resolve_target(target)
                if resolved:
                    await self._handle_text(event, self._make_video_url(resolved) or payload_text)
                    return
            if self._debug():
                logger.info("bili: json card received but not bilibili: %s", self._last_json_payload)

    async def _send_forward_bundle(self, event: AstrMessageEvent, message: str, cover: str, link: str, title: str, duration_sec: int):
        if self._enable_http_callback():
            task_id, status_msg = await self._create_download_task(link, title, duration_sec)
            if not task_id:
                if status_msg:
                    await event.send(MessageChain().message(status_msg))
                return
            self._register_callback_task(task_id, event, message, cover, link, title, duration_sec)
            asyncio.create_task(self._callback_fallback(task_id))
            return

        video_url, status_msg = await self._get_video_file_url(link, title, duration_sec)
        await self._send_forward_with_video(event, message, cover, video_url, status_msg)

    async def _send_forward_with_video(self, event: AstrMessageEvent, message: str, cover: str, video_url: str, status_msg: str = ""):
        nodes: list[Node] = []
        content: list = []
        if cover and self._show_cover():
            content.append(Image.fromURL(cover))
        if message:
            content.append(Plain(message))
        if content:
            nodes.append(Node(name="BiliWatch", uin="0", content=content))
        if video_url:
            nodes.append(Node(name="BiliWatch", uin="0", content=[Video.fromURL(video_url)]))
        if not nodes:
            if status_msg:
                await event.send(MessageChain().message(status_msg))
            return
        chain = MessageChain()
        chain.chain.append(Nodes(nodes))
        await event.send(chain)

    def _register_callback_task(
        self,
        task_id: str,
        event: AstrMessageEvent,
        message: str,
        cover: str,
        link: str,
        title: str,
        duration_sec: int,
    ):
        self._callback_tasks[task_id] = {
            "event": event,
            "message": message,
            "cover": cover,
            "link": link,
            "title": title,
            "duration_sec": duration_sec,
        }

    async def _callback_fallback(self, task_id: str):
        delay = self._callback_fallback_sec()
        if delay <= 0:
            return
        await asyncio.sleep(delay)
        info = self._callback_tasks.pop(task_id, None)
        if not info:
            return
        if self._debug():
            logger.info("bili: callback timeout, fallback to polling task_id=%s", task_id)
        base_url = str(self.config.get("video_api_base_url", "") or "").strip().rstrip("/")
        if not base_url:
            return
        video_url, status_msg = await self._poll_existing_task(
            base_url,
            task_id,
            info["link"],
            info["title"],
            info["duration_sec"],
        )
        await self._send_forward_with_video(
            info["event"],
            info["message"],
            info["cover"],
            video_url,
            status_msg,
        )

    async def _create_download_task(self, video_link: str, title: str = "", duration_sec: int = 0) -> Tuple[str, str]:
        base_url = str(self.config.get("video_api_base_url", "") or "").strip().rstrip("/")
        if not base_url:
            return "", "还没配视频API呢，想啥呢～"

        existing_name, existing_size = await self._find_existing_file(base_url, video_link, title)
        if existing_name:
            size_limit = self._max_video_file_size_bytes()
            if size_limit > 0 and isinstance(existing_size, int) and existing_size > size_limit:
                mb = existing_size / 1024 / 1024
                return "", f"文件太大啦（{mb:.2f}MB），不准发～"
            return "", "已经有成品啦，直接 /bili_file send 发就是了。"

        size_limit = self._max_video_file_size_bytes()
        chosen_index = self._video_quality_index()
        if size_limit > 0 and duration_sec > 0:
            chosen_index, _, hint_msg = await self._pick_quality_by_size(
                base_url, video_link, duration_sec, size_limit, chosen_index
            )
            if hint_msg:
                return "", hint_msg

        filename_hint = self._sanitize_filename(title)
        params = {
            "url": video_link,
            "merge": "true",
            "video_quality": str(chosen_index),
        }
        if filename_hint:
            params["filename"] = filename_hint

        status, text = await self._request_text(f"{base_url}/api/video/download", params)
        if status == 409:
            m_exist = re.search(r"已存在任务ID[:：]\s*([a-f0-9\\-]{8,})", text, re.I)
            if m_exist:
                return m_exist.group(1), ""
            return "", "创建下载任务失败：HTTP 409（任务已存在）"
        if status != 200 or not text:
            if status == 599:
                return "", "创建下载任务失败：HTTP 599（网络错误/超时/无法连接API）"
            return "", f"创建下载任务失败：HTTP {status}"
        m = re.search(r"任务ID[:：]\s*([a-f0-9\\-]{8,})", text, re.I)
        if not m:
            return "", "任务ID都没拿到，真麻烦。"
        return m.group(1), ""

    def _ensure_callback_server(self):
        if not self._enable_http_callback():
            return
        if self._callback_server is not None:
            return
        port = self._callback_port()
        if port <= 0:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except Exception:
            return
        token = self._callback_token()
        path = self._callback_path()
        plugin = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                if self.path != path:
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = None
                if token:
                    header_token = self.headers.get("X-Callback-Token", "")
                    body_token = payload.get("token") if isinstance(payload, dict) else ""
                    if header_token != token and body_token != token:
                        logger.warning("bili: callback forbidden (token mismatch)")
                        self.send_response(403)
                        self.end_headers()
                        return
                plugin._schedule_callback(payload)
                self.send_response(200)
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A003
                return

        try:
            server = ThreadingHTTPServer((self._callback_host(), port), _Handler)
        except Exception as exc:
            if self._debug():
                logger.error("bili: callback server start failed: %s", exc)
            return
        self._callback_server = server
        self._callback_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._callback_thread.start()

    def _schedule_callback(self, payload: Any):
        if not isinstance(payload, dict):
            return
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._handle_callback(payload), self._loop)

    async def _handle_callback(self, payload: Dict[str, Any]):
        status = str(payload.get("status", "") or "").lower()
        if status not in {"completed", "done", "success", "finished"}:
            return
        task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
        if not task_id:
            return
        if self._debug():
            logger.info("bili: callback received task_id=%s", task_id)
        info = self._callback_tasks.pop(task_id, None)
        if not info:
            return
        event = info.get("event")
        if not event:
            return
        base_url = self._api_base()
        filename = str(payload.get("filename") or payload.get("file_name") or "").strip()
        video_url = ""
        if filename:
            size_limit = self._max_video_file_size_bytes()
            if size_limit > 0:
                size_map = await self._get_files_size_map(base_url)
                size = size_map.get(filename)
                if size is not None and size > size_limit:
                    mb = size / 1024 / 1024
                    await event.send(MessageChain().message(f"文件太大啦（{mb:.2f}MB），不准发～"))
                    return
            video_url = f"{base_url}/api/files/{filename}"
        else:
            video_url = f"{base_url}/api/download/file/{task_id}"

        await self._send_forward_with_video(
            event,
            info.get("message") or "",
            info.get("cover") or "",
            video_url,
            "",
        )

    async def _send_video_file_later(self, event: AstrMessageEvent, link: str, title: str = ""):
        video_url, status_msg = await self._get_video_file_url(link, title)
        if video_url:
            video_chain = MessageChain()
            video_chain.chain.append(Video.fromURL(video_url))
            await event.send(video_chain)
        else:
            await event.send(MessageChain().message(status_msg or "没拿到视频呢，就看图文吧。"))

    def _make_video_url(self, target: VideoTarget) -> str:
        if target.bvid:
            return f"https://www.bilibili.com/video/{target.bvid}"
        if target.aid:
            return f"https://www.bilibili.com/video/av{target.aid}"
        return ""

    def _parse_custom_text(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        title = up = duration = play = danmaku = reply = favorite = like = desc = cover = link = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("标题："):
                title = line.replace("标题：", "", 1).strip()
            elif line.startswith("UP主："):
                up = line.replace("UP主：", "", 1).strip()
            elif line.startswith("时长："):
                duration = line.replace("时长：", "", 1).strip()
            elif line.startswith("播放："):
                play_part = line.replace("播放：", "", 1).strip()
                # 播放：2.9万 | 弹幕：368 | 评论：14
                parts = [p.strip() for p in play_part.split("|")]
                if len(parts) >= 1:
                    play = parts[0].replace("播放", "").strip()
                if len(parts) >= 2:
                    danmaku = parts[1].replace("弹幕：", "").strip()
                if len(parts) >= 3:
                    reply = parts[2].replace("评论：", "").strip()
            elif line.startswith("收藏："):
                fav_part = line.replace("收藏：", "", 1).strip()
                parts = [p.strip() for p in fav_part.split("|")]
                if len(parts) >= 1:
                    favorite = parts[0].strip()
                if len(parts) >= 2:
                    like = parts[1].replace("点赞：", "").strip()
            elif line.startswith("简介："):
                desc = line.replace("简介：", "", 1).strip()
            elif line.startswith("封面："):
                cover = line.replace("封面：", "", 1).strip()
            elif line.startswith("链接："):
                link = line.replace("链接：", "", 1).strip()

        if not title:
            return None

        lines = [
            f"标题：{title}",
            f"UP主：{up}" if up else "UP主：未知",
            f"时长：{duration}" if duration else "时长：未知",
        ]
        if play or danmaku or reply:
            play_val = play or "0"
            danmaku_val = danmaku or "0"
            reply_val = reply or "0"
            lines.append(f"播放：{play_val} | 弹幕：{danmaku_val} | 评论：{reply_val}")
        if favorite or like:
            fav_val = favorite or "0"
            like_val = like or "0"
            lines.append(f"收藏：{fav_val} | 点赞：{like_val}")
        if desc:
            lines.append(f"简介：{self._shorten(desc, 80)}")

        duration_sec = self._parse_duration_str_to_seconds(duration)
        return {"text": "\n".join(lines), "cover": cover, "link": link, "duration_sec": duration_sec, "title": title}

    async def _get_video_file_url(self, video_link: str, title: str = "", duration_sec: int = 0) -> Tuple[str, str]:
        base_url = str(self.config.get("video_api_base_url", "") or "").strip().rstrip("/")
        if not base_url:
            return "", "还没配视频API呢，想啥呢～"

        existing_name, existing_size = await self._find_existing_file(base_url, video_link, title)
        if existing_name:
            size_limit = self._max_video_file_size_bytes()
            if size_limit > 0 and isinstance(existing_size, int) and existing_size > size_limit:
                mb = existing_size / 1024 / 1024
                return "", f"文件太大啦（{mb:.2f}MB），不准发～"
            return f"{base_url}/api/files/{existing_name}", ""

        filename_hint = self._sanitize_filename(title)
        size_limit = self._max_video_file_size_bytes()
        chosen_index = self._video_quality_index()
        if size_limit > 0 and duration_sec > 0:
            chosen_index, est_bytes, hint_msg = await self._pick_quality_by_size(
                base_url, video_link, duration_sec, size_limit, chosen_index
            )
            if hint_msg:
                return "", hint_msg
        params = {
            "url": video_link,
            "merge": "true",
            "video_quality": str(chosen_index),
        }
        if filename_hint:
            params["filename"] = filename_hint

        # 1) create download task
        status, text = await self._request_text(f"{base_url}/api/video/download", params)
        if status == 409:
            # 任务已存在，尝试从提示中复用任务ID
            m_exist = re.search(r"已存在任务ID[:：]\s*([a-f0-9\\-]{8,})", text, re.I)
            if m_exist:
                task_id = m_exist.group(1)
            else:
                return "", "创建下载任务失败：HTTP 409（任务已存在）"
        elif status != 200 or not text:
            if status == 599:
                return "", "创建下载任务失败：HTTP 599（网络错误/超时/无法连接API）"
            return "", f"创建下载任务失败：HTTP {status}"
        else:
            m = re.search(r"任务ID[:：]\s*([a-f0-9\\-]{8,})", text, re.I)
            if not m:
                return "", "任务ID都没拿到，真麻烦。"
            task_id = m.group(1)

        # 2) poll status
        return await self._poll_existing_task(base_url, task_id, video_link, title, duration_sec)

    async def _poll_existing_task(
        self,
        base_url: str,
        task_id: str,
        video_link: str,
        title: str,
        duration_sec: int,
    ) -> Tuple[str, str]:
        timeout_sec = self._video_download_timeout_sec()
        deadline = None
        if timeout_sec > 0:
            deadline = asyncio.get_event_loop().time() + timeout_sec
        last_progress = None
        stall_count = 0
        while True:
            if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                break
            s, st = await self._request_text(f"{base_url}/api/download/status/{task_id}", {})
            if s == 404:
                existing_name, existing_size = await self._find_existing_file(base_url, video_link, title)
                if existing_name:
                    size_limit = self._max_video_file_size_bytes()
                    if size_limit > 0 and isinstance(existing_size, int) and existing_size > size_limit:
                        mb = existing_size / 1024 / 1024
                        return "", f"文件太大啦（{mb:.2f}MB），不准发～"
                    return f"{base_url}/api/files/{existing_name}", ""
                return "", "任务不存在，已停止轮询。"
            if s == 200:
                status_upper = str(st).upper()
                if "失败" in st or "FAILED" in status_upper:
                    await self._cancel_download_task(base_url, task_id)
                    return "", "下载失败了，别指望我。"
                if "已完成" in st or "COMPLETED" in status_upper:
                    size_limit = self._max_video_file_size_bytes()
                    if size_limit > 0:
                        ok, msg = await self._enforce_file_size_limit(base_url, task_id, size_limit)
                        if not ok:
                            return "", msg
                    return f"{base_url}/api/download/file/{task_id}", ""
                prog = self._parse_progress(st)
                if prog is not None:
                    if last_progress is None or prog > last_progress:
                        last_progress = prog
                        stall_count = 0
                    else:
                        stall_count += 1
                        if stall_count >= self._video_progress_stall_limit():
                            await self._cancel_download_task(base_url, task_id)
                            return "", "进度不对劲，懒得等了。"
            await asyncio.sleep(self._video_poll_interval_sec())

        if deadline is not None:
            await self._cancel_download_task(base_url, task_id)
            return "", "超时啦，只给你图文。"
        return "", "下载失败了，别指望我。"

    async def _pick_quality_by_size(
        self,
        base_url: str,
        link: str,
        duration_sec: int,
        size_limit: int,
        preferred_index: int,
    ) -> Tuple[int, int, str]:
        status, data = await self._request_json(f"{base_url}/api/video/quality/json", {"url": link})
        if status != 200 or not isinstance(data, dict) or not data.get("success"):
            return preferred_index, 0, ""
        options = data.get("data", {}) or {}
        vlist = options.get("video_options", []) or []
        alist = options.get("audio_options", []) or []
        if not vlist or not alist:
            return preferred_index, 0, ""
        aopt = next((a for a in alist if int(a.get("index", -1)) == 0), None) or alist[0]

        def estimate(index: int) -> int:
            vopt = next((v for v in vlist if int(v.get("index", -1)) == index), None)
            if not vopt or not aopt:
                return 0
            try:
                v_bps = int(vopt.get("bandwidth", 0))
                a_bps = int(aopt.get("bandwidth", 0))
            except Exception:
                return 0
            if v_bps <= 0 and a_bps <= 0:
                return 0
            total_bps = v_bps + a_bps
            return int(duration_sec * total_bps / 8)

        est = estimate(preferred_index)
        if est == 0 or est <= size_limit:
            return preferred_index, est, ""

        if self._auto_lower_quality():
            lowest_index = max(int(v.get("index", 0)) for v in vlist)
            est_low = estimate(lowest_index)
            if est_low > 0 and est_low <= size_limit:
                return lowest_index, est_low, ""
            mb = est_low / 1024 / 1024 if est_low > 0 else 0
            return preferred_index, est, f"文件太大啦（约{mb:.2f}MB），不准下～"

        mb = est / 1024 / 1024
        return preferred_index, est, f"文件太大啦（约{mb:.2f}MB），不准下～"

    async def _enforce_file_size_limit(self, base_url: str, task_id: str, limit_bytes: int) -> Tuple[bool, str]:
        filename = await self._get_task_filename(base_url, task_id)
        if not filename:
            return True, ""
        size_map = await self._get_files_size_map(base_url)
        size = size_map.get(filename)
        if size is None:
            return True, ""
        if size > limit_bytes:
            await self._request_json_method("DELETE", f"{base_url}/api/files/{filename}", {})
            mb = size / 1024 / 1024
            limit_mb = limit_bytes / 1024 / 1024
            return False, f"文件太大啦（{mb:.2f}MB），不准发～"
        return True, ""

    async def _get_task_filename(self, base_url: str, task_id: str) -> str:
        status, data = await self._request_json(f"{base_url}/api/tasks/json", {})
        if status != 200 or not isinstance(data, dict):
            return ""
        task = (data.get("tasks") or {}).get(task_id) or {}
        file_path = task.get("file_path") or task.get("video_path") or ""
        if not file_path:
            return ""
        return file_path.replace("\\", "/").split("/")[-1]

    async def _get_files_size_map(self, base_url: str) -> Dict[str, int]:
        status, data = await self._request_json(f"{base_url}/api/files", {})
        if status != 200 or not isinstance(data, dict):
            return {}
        files = data.get("files", []) or []
        result: Dict[str, int] = {}
        for f in files:
            name = f.get("name")
            size = f.get("size")
            if name and isinstance(size, int):
                result[name] = size
        return result

    def _extract_bvid_from_link(self, link: str) -> str:
        if not link:
            return ""
        m = BV_REGEX.search(link)
        if not m:
            return ""
        return self._normalize_bvid(m.group(1))

    async def _find_existing_file(self, base_url: str, link: str, title: str) -> Tuple[str, Optional[int]]:
        status, data = await self._request_json(f"{base_url}/api/files", {})
        if status != 200 or not isinstance(data, dict):
            return "", None
        files = data.get("files", []) or []
        if not files:
            return "", None
        bvid = self._extract_bvid_from_link(link)
        title_hint = self._sanitize_filename(title)
        candidates = []
        for f in files:
            name = f.get("name") or ""
            if not name:
                continue
            size = f.get("size") if isinstance(f.get("size"), int) else None
            if bvid and bvid in name:
                candidates.append((name, size, 2))
                continue
            if title_hint and title_hint in name:
                candidates.append((name, size, 1))
        if not candidates:
            return "", None
        candidates.sort(key=lambda x: (-x[2], len(x[0])))
        best = candidates[0]
        return best[0], best[1]

    async def _cancel_download_task(self, base_url: str, task_id: str):
        try:
            await self._request_json_method("POST", f"{base_url}/api/tasks/cancel/{task_id}", {})
        except Exception:
            return

    def _parse_progress(self, text: str) -> Optional[int]:
        m = re.search(r"进度[:：]\\s*(\\d+)%", text)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
        return None

    def _video_download_timeout_sec(self) -> int:
        try:
            return int(self.config.get("video_download_timeout_sec", 20))
        except Exception:
            return 20

    def _video_progress_stall_limit(self) -> int:
        try:
            return int(self.config.get("video_progress_stall_limit", 3))
        except Exception:
            return 3

    def _video_poll_interval_sec(self) -> float:
        try:
            return float(self.config.get("video_poll_interval_sec", 1))
        except Exception:
            return 1.0

    def _max_video_duration_sec(self) -> int:
        try:
            minutes = int(self.config.get("max_video_duration_sec", 0))
        except Exception:
            return 0
        if minutes <= 0:
            return 0
        return minutes * 60

    def _max_video_file_size_mb(self) -> int:
        try:
            return int(self.config.get("max_video_file_size_mb", 0))
        except Exception:
            return 0

    def _max_video_file_size_bytes(self) -> int:
        mb = self._max_video_file_size_mb()
        if mb <= 0:
            return 0
        return mb * 1024 * 1024

    def _video_quality_index(self) -> int:
        try:
            preset = int(self.config.get("video_quality_preset", 0))
        except Exception:
            preset = 0
        # -1=low, 0=mid, 1=high (API uses 0 as highest)
        if preset <= -1:
            return 2
        if preset >= 1:
            return 0
        return 1

    def _auto_lower_quality(self) -> bool:
        return bool(self.config.get("auto_lower_quality_on_oversize", False))

    def _parse_duration_to_seconds(self, value: Any) -> int:
        try:
            return int(value)
        except Exception:
            return 0

    def _parse_duration_str_to_seconds(self, value: str) -> int:
        if not value:
            return 0
        # H:MM:SS or M:SS
        parts = value.split(":")
        try:
            if len(parts) == 3:
                h, m, s = [int(x) for x in parts]
                return h * 3600 + m * 60 + s
            if len(parts) == 2:
                m, s = [int(x) for x in parts]
                return m * 60 + s
        except Exception:
            return 0
        return 0

    def _sanitize_filename(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return ""
        # remove illegal filename chars
        name = re.sub(r"[\\\\/:*?\"<>|]", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if len(name) > 80:
            name = name[:80].strip()
        return name

    @filter.command("bili_login")
    async def bili_login_cmd(self, event: AstrMessageEvent, action: str = "", key: str = ""):
        action = (action or "").strip().lower()
        if not action:
            return event.plain_result("用法：/bili_login <start|status> [qrcode_key]")
        base_url = self._api_base()
        token = self._api_token()
        if not base_url:
            return event.plain_result("先配 video_api_base_url，再来。")
        if not token:
            return event.plain_result("token都没配，还想登录？")

        if action == "start":
            status, data = await self._request_json(f"{base_url}/api/login/qr", {"token": token})
            if status != 200 or not isinstance(data, dict) or not data.get("success"):
                return event.plain_result(f"二维码拿不到，HTTP {status}。")
            qrcode_key = data.get("qrcode_key", "")
            url = data.get("url", "")
            image_url = data.get("image_url", "")
            chain = MessageChain()
            if image_url:
                chain.url_image(f"{base_url}{image_url}")
            msg = f"登录二维码已生成\nqrcode_key: {qrcode_key}\nURL: {url}\n\n查询状态：/bili_login status {qrcode_key}"
            chain.message(msg)
            await event.send(chain)
            return None

        if action == "status":
            if not key:
                return event.plain_result("用法：/bili_login status <qrcode_key>")
            status, data = await self._request_json(f"{base_url}/api/login/status", {"token": token, "qrcode_key": key})
            if status != 200 or not isinstance(data, dict):
                return event.plain_result(f"查询失败：HTTP {status}")
            if data.get("success") and data.get("status") == "success":
                cookie_str = str(data.get("cookie", "") or "").strip()
                if cookie_str:
                    self.config["bilibili_cookie"] = cookie_str
                    self.config.save_config()
                return event.plain_result("登录成功了，cookies 已塞进配置。")
            return event.plain_result(f"还没好哦：{data.get('status')} | {data.get('message')}")

        return event.plain_result("用法：/bili_login <start|status> [qrcode_key]")

    @filter.command("bili_menu")
    async def bili_menu_cmd(self, event: AstrMessageEvent, module: str = ""):
        if module:
            return await self._send_plain_once(event, self._submenu_text(module))
        return await self._send_plain_once(event, self._menu_text())

    @filter.command("bili_task")
    async def bili_task_cmd(self, event: AstrMessageEvent, action: str = "", task_id: str = ""):
        action = (action or "").strip().lower()
        base_url = self._api_base()
        if not base_url:
            return event.plain_result("先配 video_api_base_url。")
        if action.isdigit():
            task_id = ""
            action = "list"
        if action in {"list", "ls", ""}:
            status, data = await self._request_json(f"{base_url}/api/tasks/json", {})
            if status != 200 or not isinstance(data, dict):
                return event.plain_result(f"任务列表拿不到：HTTP {status}")
            tasks = data.get("tasks", {}) or {}
            if not tasks:
                return event.plain_result("现在没任务，别急。")
            status_filter = ""
            if task_id and not task_id.isdigit():
                status_filter = self._normalize_task_status(task_id)
            ordered = sorted(tasks.items(), key=lambda x: str(x[0]))
            if status_filter:
                def _match_status(item):
                    raw = str(item[1].get("status", "unknown")).lower()
                    if status_filter == "running":
                        return raw in {"running", "downloading", "processing", "active"}
                    if status_filter == "completed":
                        return raw in {"completed", "done", "success", "finished"}
                    if status_filter == "canceled":
                        return raw in {"canceled", "cancelled", "stopped"}
                    if status_filter == "failed":
                        return raw in {"failed", "error"}
                    if status_filter == "pending":
                        return raw in {"pending", "queued", "waiting"}
                    if status_filter == "unknown":
                        return raw not in {"running", "downloading", "processing", "active",
                                           "completed", "done", "success", "finished",
                                           "canceled", "cancelled", "stopped",
                                           "failed", "error",
                                           "pending", "queued", "waiting"}
                    return False
                ordered = [item for item in ordered if _match_status(item)]
                if not ordered:
                    return event.plain_result("没有匹配该标签的任务。")

            page = self._parse_page(task_id or "1")
            page, total_pages, page_items = self._paginate(ordered, page)
            header = f"下载任务列表（第 {page}/{total_pages} 页）："
            if status_filter:
                header = f"下载任务列表（标签过滤：{task_id}，第 {page}/{total_pages} 页）："
            lines = [header]
            base_index = (page - 1) * self._page_size()
            for offset, (tid, t) in enumerate(page_items, start=1):
                status_text = t.get("status", "unknown")
                progress = t.get("progress", 0)
                label = self._task_status_label(status_text, progress)
                lines.append(f"- [{base_index + offset}] {tid} | {label}")
            lines.append("用法：/bili_task list <页码> | /bili_task list <标签> | /bili_task cancel <序号> | /bili_task canceltag <标签>")
            return await self._send_plain_once(event, "\n".join(lines))
        if action in {"clear", "all", "purge"}:
            status, data = await self._request_json(f"{base_url}/api/tasks/json", {})
            if status != 200 or not isinstance(data, dict):
                return event.plain_result(f"任务列表拿不到：HTTP {status}")
            tasks = data.get("tasks", {}) or {}
            if not tasks:
                return event.plain_result("没任务可取消呢。")
            cancelled = 0
            failed = 0
            for tid in tasks.keys():
                s, _ = await self._request_json_method("POST", f"{base_url}/api/tasks/cancel/{tid}", {})
                if s == 200:
                    cancelled += 1
                else:
                    failed += 1
            return event.plain_result(f"批量取消完成：成功 {cancelled} 个，失败 {failed} 个。")
        if action in {"del", "remove"}:
            if not task_id:
                return event.plain_result("用法：/bili_task del <序号>")
            if task_id.isdigit():
                status, data = await self._request_json(f"{base_url}/api/tasks/json", {})
                if status != 200 or not isinstance(data, dict):
                    return event.plain_result(f"任务列表拿不到：HTTP {status}")
                ordered = sorted((data.get("tasks") or {}).items(), key=lambda x: str(x[0]))
                idx = int(task_id)
                if idx < 1 or idx > len(ordered):
                    return event.plain_result("序号超出范围。")
                task_id = ordered[idx - 1][0]
            s, d = await self._request_json_method("POST", f"{base_url}/api/tasks/remove/{task_id}", {})
            if s == 200:
                return event.plain_result("任务已删除。")
            return event.plain_result(f"删除失败：HTTP {s}")
        if action in {"cancel", "stop", "rm"}:
            if not task_id:
                return event.plain_result("用法：/bili_task cancel <序号>")
            if task_id.isdigit():
                status, data = await self._request_json(f"{base_url}/api/tasks/json", {})
                if status != 200 or not isinstance(data, dict):
                    return event.plain_result(f"任务列表拿不到：HTTP {status}")
                ordered = sorted((data.get("tasks") or {}).items(), key=lambda x: str(x[0]))
                idx = int(task_id)
                if idx < 1 or idx > len(ordered):
                    return event.plain_result("序号超出范围。")
                task_id, task_obj = ordered[idx - 1]
                if str(task_obj.get("status")) == "completed":
                    s, d = await self._request_json_method("POST", f"{base_url}/api/tasks/remove/{task_id}", {})
                    if s == 200:
                        return event.plain_result("任务已结束，顺手帮你清掉了。")
                    return event.plain_result(f"删除失败：HTTP {s}")
            else:
                status, data = await self._request_json(f"{base_url}/api/tasks/json", {})
                if status == 200 and isinstance(data, dict):
                    task_obj = (data.get("tasks") or {}).get(task_id) or {}
                    if str(task_obj.get("status")) == "completed":
                        s, d = await self._request_json_method("POST", f"{base_url}/api/tasks/remove/{task_id}", {})
                        if s == 200:
                            return event.plain_result("任务已结束，顺手帮你清掉了。")
                        return event.plain_result(f"删除失败：HTTP {s}")
            status, data = await self._request_json_method("POST", f"{base_url}/api/tasks/cancel/{task_id}", {})
            if status != 200:
                return event.plain_result(f"取消失败：HTTP {status}")
            msg = data.get("message") if isinstance(data, dict) else ""
            return event.plain_result(msg or "取消丢出去了。")
        if action in {"canceltag", "cancel_tag"}:
            if not task_id:
                return event.plain_result("用法：/bili_task canceltag <标签>")
            status_filter = self._normalize_task_status(task_id)
            if not status_filter:
                return event.plain_result("未知标签，可用：完成/下载/取消/失败/排队/未知")
            status, data = await self._request_json(f"{base_url}/api/tasks/json", {})
            if status != 200 or not isinstance(data, dict):
                return event.plain_result(f"任务列表拿不到：HTTP {status}")
            tasks = data.get("tasks", {}) or {}
            if not tasks:
                return event.plain_result("现在没任务，别急。")
            def _match_status(item):
                raw = str(item[1].get("status", "unknown")).lower()
                if status_filter == "running":
                    return raw in {"running", "downloading", "processing", "active"}
                if status_filter == "completed":
                    return raw in {"completed", "done", "success", "finished"}
                if status_filter == "canceled":
                    return raw in {"canceled", "cancelled", "stopped"}
                if status_filter == "failed":
                    return raw in {"failed", "error"}
                if status_filter == "pending":
                    return raw in {"pending", "queued", "waiting"}
                if status_filter == "unknown":
                    return raw not in {"running", "downloading", "processing", "active",
                                       "completed", "done", "success", "finished",
                                       "canceled", "cancelled", "stopped",
                                       "failed", "error",
                                       "pending", "queued", "waiting"}
                return False
            targets = [item for item in tasks.items() if _match_status(item)]
            if not targets:
                return event.plain_result("没有匹配该标签的任务。")
            canceled = 0
            skipped = 0
            failed = 0
            for tid, task_obj in targets:
                raw = str(task_obj.get("status", "unknown")).lower()
                if raw in {"completed", "done", "success", "finished"}:
                    skipped += 1
                    continue
                s, _ = await self._request_json_method("POST", f"{base_url}/api/tasks/cancel/{tid}", {})
                if s == 200:
                    canceled += 1
                else:
                    failed += 1
            return event.plain_result(f"按标签取消完成：成功 {canceled} 个，跳过 {skipped} 个，失败 {failed} 个。")
        if action in {"deltag", "del_tag", "rm_tag"}:
            if not task_id:
                return event.plain_result("用法：/bili_task deltag <标签>")
            status_filter = self._normalize_task_status(task_id)
            if not status_filter:
                return event.plain_result("未知标签，可用：完成/下载/取消/失败/排队/未知")
            status, data = await self._request_json(f"{base_url}/api/tasks/json", {})
            if status != 200 or not isinstance(data, dict):
                return event.plain_result(f"任务列表拿不到：HTTP {status}")
            tasks = data.get("tasks", {}) or {}
            if not tasks:
                return event.plain_result("现在没任务，别急。")
            def _match_status(item):
                raw = str(item[1].get("status", "unknown")).lower()
                if status_filter == "running":
                    return raw in {"running", "downloading", "processing", "active"}
                if status_filter == "completed":
                    return raw in {"completed", "done", "success", "finished"}
                if status_filter == "canceled":
                    return raw in {"canceled", "cancelled", "stopped"}
                if status_filter == "failed":
                    return raw in {"failed", "error"}
                if status_filter == "pending":
                    return raw in {"pending", "queued", "waiting"}
                if status_filter == "unknown":
                    return raw not in {"running", "downloading", "processing", "active",
                                       "completed", "done", "success", "finished",
                                       "canceled", "cancelled", "stopped",
                                       "failed", "error",
                                       "pending", "queued", "waiting"}
                return False
            targets = [item for item in tasks.items() if _match_status(item)]
            if not targets:
                return event.plain_result("没有匹配该标签的任务。")
            deleted = 0
            failed = 0
            for tid, task_obj in targets:
                raw = str(task_obj.get("status", "unknown")).lower()
                if raw in {"completed", "done", "success", "finished"}:
                    s, _ = await self._request_json_method("POST", f"{base_url}/api/tasks/remove/{tid}", {})
                else:
                    s, _ = await self._request_json_method("POST", f"{base_url}/api/tasks/cancel/{tid}", {})
                if s == 200:
                    deleted += 1
                else:
                    failed += 1
            return event.plain_result(f"按标签清理完成：成功 {deleted} 个，失败 {failed} 个。")
        return event.plain_result("用法：/bili_task <list|cancel|del|deltag|canceltag|clear> [task_id]")

    @filter.command("bili_file")
    async def bili_file_cmd(self, event: AstrMessageEvent, action: str = "", filename: str = ""):
        action = (action or "").strip().lower()
        base_url = self._api_base()
        if not base_url:
            return event.plain_result("先配 video_api_base_url。")
        if action.isdigit():
            filename = ""
            action = "list"
        if action in {"list", "ls", ""}:
            status, data = await self._request_json(f"{base_url}/api/files", {})
            if status != 200 or not isinstance(data, dict):
                return event.plain_result(f"文件列表拿不到：HTTP {status}")
            files = data.get("files", []) or []
            if not files:
                return event.plain_result("现在没文件，别急。")
            ordered = sorted(files, key=lambda x: str(x.get("name", "")))
            label_filter = ""
            if filename and not filename.isdigit():
                label_filter = self._normalize_file_label(filename)
                if not label_filter:
                    return event.plain_result("未知标签，可用：完成")
            page = self._parse_page(filename or "1")
            page, total_pages, page_items = self._paginate(ordered, page)
            header = f"已下载文件（第 {page}/{total_pages} 页）："
            if label_filter:
                header = f"已下载文件（标签过滤：{filename}，第 {page}/{total_pages} 页）："
            lines = [header]
            base_index = (page - 1) * self._page_size()
            for offset, f in enumerate(page_items, start=1):
                label = self._file_status_label(f.get("name"), f.get("size"))
                lines.append(f"- [{base_index + offset}] {f.get('name')} | {label}")
            lines.append("用法：/bili_file list <页码> | /bili_file send <序号> | /bili_file del <序号>")
            return await self._send_plain_once(event, "\n".join(lines))
        if action in {"send", "play"}:
            if not filename:
                return event.plain_result("用法：/bili_file send <序号>")
            if filename.isdigit():
                status, data = await self._request_json(f"{base_url}/api/files", {})
                if status != 200 or not isinstance(data, dict):
                    return event.plain_result(f"文件列表拿不到：HTTP {status}")
                ordered = sorted(data.get("files", []) or [], key=lambda x: str(x.get("name", "")))
                idx = int(filename)
                if idx < 1 or idx > len(ordered):
                    return event.plain_result("序号超出范围。")
                filename = ordered[idx - 1].get("name") or ""
                if not filename:
                    return event.plain_result("序号对应的文件不存在。")
            size_limit = self._max_video_file_size_bytes()
            if size_limit > 0:
                size_map = await self._get_files_size_map(base_url)
                size = size_map.get(filename)
                if size is not None and size > size_limit:
                    mb = size / 1024 / 1024
                    return event.plain_result(f"文件太大啦（{mb:.2f}MB），不准发～")
            video_chain = MessageChain()
            video_chain.chain.append(Video.fromURL(f"{base_url}/api/files/{filename}"))
            await event.send(video_chain)
            return None
        if action in {"clear", "all", "purge"}:
            status, data = await self._request_json(f"{base_url}/api/files", {})
            if status != 200 or not isinstance(data, dict):
                return event.plain_result(f"文件列表拿不到：HTTP {status}")
            files = data.get("files", []) or []
            if not files:
                return event.plain_result("没文件可删。")
            deleted = 0
            failed = 0
            for f in files:
                name = f.get("name")
                if not name:
                    continue
                s, _ = await self._request_json_method("DELETE", f"{base_url}/api/files/{name}", {})
                if s == 200:
                    deleted += 1
                else:
                    failed += 1
            return event.plain_result(f"批量删除完成：成功 {deleted} 个，失败 {failed} 个。")
        if action in {"deltag", "del_tag", "rm_tag"}:
            if not filename:
                return event.plain_result("用法：/bili_file deltag <标签>")
            label_filter = self._normalize_file_label(filename)
            if not label_filter:
                return event.plain_result("未知标签，可用：完成")
            status, data = await self._request_json(f"{base_url}/api/files", {})
            if status != 200 or not isinstance(data, dict):
                return event.plain_result(f"文件列表拿不到：HTTP {status}")
            files = data.get("files", []) or []
            if not files:
                return event.plain_result("没文件可删。")
            deleted = 0
            failed = 0
            for f in files:
                name = f.get("name")
                if not name:
                    continue
                s, _ = await self._request_json_method("DELETE", f"{base_url}/api/files/{name}", {})
                if s == 200:
                    deleted += 1
                else:
                    failed += 1
            return event.plain_result(f"按标签清理完成：成功 {deleted} 个，失败 {failed} 个。")
        if action in {"del", "rm", "remove"}:
            if not filename:
                return event.plain_result("用法：/bili_file del <序号>")
            if filename.isdigit():
                status, data = await self._request_json(f"{base_url}/api/files", {})
                if status != 200 or not isinstance(data, dict):
                    return event.plain_result(f"文件列表拿不到：HTTP {status}")
                ordered = sorted(data.get("files", []) or [], key=lambda x: str(x.get("name", "")))
                idx = int(filename)
                if idx < 1 or idx > len(ordered):
                    return event.plain_result("序号超出范围。")
                filename = ordered[idx - 1].get("name") or ""
                if not filename:
                    return event.plain_result("序号对应的文件不存在。")
            status, data = await self._request_json_method("DELETE", f"{base_url}/api/files/{filename}", {})
            if status != 200:
                return event.plain_result(f"删除失败：HTTP {status}")
            msg = data.get("message") if isinstance(data, dict) else ""
            return event.plain_result(msg or "删掉啦。")
        return event.plain_result("用法：/bili_file <list|send|del|deltag|clear> [filename]")

    def _menu_text(self) -> str:
        return (
            "========== BiliWatch 菜单 ==========\n"
            "简化入口：/bili_menu <模块>\n"
            "模块列表：parse / login / task / file / api / net / config\n"
            "--------------------------------------\n"
            "【解析】/bili_menu parse  - 自动解析/番剧/小程序\n"
            "【登录】/bili_menu login  - 扫码登录\n"
            "【任务】/bili_menu task   - 下载任务\n"
            "【文件】/bili_menu file   - 已下载文件\n"
            "【接口】/bili_menu api    - 解析API配置\n"
            "【网络】/bili_menu net    - 连通性测试\n"
            "【配置】/bili_menu config - 配置说明\n"
            "--------------------------------------\n"
            "示例：/bili_menu task\n"
            "完整命令：/bili help"
        )

    def _submenu_text(self, module: str) -> str:
        module = (module or "").strip().lower()
        if module in {"parse", "解析"}:
            return (
                "【解析】\n"
                "- 自动解析群内B站链接\n"
                "- 支持 BV/AV/短链/番剧(ep/ss)/小程序卡片\n"
                "- 直接丢链接即可，无需额外指令\n"
                "- /bili_debug 查看最近一次小程序卡片\n"
            )
        if module in {"login", "登录"}:
            return (
                "【登录】(需配置 video_api_base_url + token)\n"
                "- /bili_login start  获取二维码\n"
                "- /bili_login status <qrcode_key> 查询状态\n"
                "- 用于获取登录 Cookie，增强解析能力\n"
            )
        if module in {"task", "任务"}:
            return (
                "【任务】\n"
                "- /bili_task list [页码]\n"
                "- /bili_task list <标签>  按标签筛选\n"
                "- /bili_task cancel <序号>\n"
                "- /bili_task del <序号>\n"
                "- /bili_task canceltag <标签>  按标签取消\n"
                "- /bili_task deltag <标签>  按标签删除\n"
                "- /bili_task clear\n"
                "标签说明：\n"
                "✅ 已完成 | ⬇️ 下载中 | 🟨 已取消 | ❌ 失败 | ⏳ 排队中 | ❓ 未知\n"
                "标签别名：完成/下载/取消/失败/排队/未知\n"
            )
        if module in {"file", "文件"}:
            return (
                "【文件】\n"
                "- /bili_file list [页码]\n"
                "- /bili_file list <标签>\n"
                "- /bili_file send <序号>\n"
                "- /bili_file del <序号>\n"
                "- /bili_file deltag <标签>\n"
                "- /bili_file clear\n"
                "标签说明：\n"
                "✅ 已完成 | 文件大小(MB)\n"
                "标签别名：完成\n"
            )
        if module in {"api", "接口", "站点"}:
            return (
                "【接口】\n"
                "- /bili_api show  查看当前 API 配置\n"
                "- /bili_api set <base_url> [token]\n"
                "说明：\n"
                "- base_url 例如 http://127.0.0.1:8000\n"
                "- token 可选，留空则仅更新地址\n"
            )
        if module in {"net", "网络"}:
            return (
                "【网络】\n"
                "- /bili_net  连通性测试\n"
                "说明：\n"
                "- 检查官方接口/自建 API/回调监听\n"
            )
        if module in {"config", "配置"}:
            return (
                "【配置说明】\n"
                "- video_api_base_url    自建解析API地址\n"
                "- video_api_login_token 登录令牌\n"
                "- enable_video_output   是否发送视频文件\n"
                "- show_cover            是否显示封面\n"
                "- max_video_duration_sec 视频时长限制(分钟)\n"
                "- max_video_file_size_mb 文件大小限制(MB)\n"
                "- video_quality_preset  画质(-1/0/1)\n"
                "- enable_http_callback  启用回调推送视频\n"
                "- callback_listen_host  回调监听地址\n"
                "- callback_listen_port  回调监听端口\n"
                "- callback_token        回调令牌\n"
                "- /bili_callback sync   同步回调到API\n"
                "- /bili_callback set <token> 设置回调令牌\n"
                "说明：\n"
                "- video_api_login_token 用于调用 API 登录/配置接口\n"
                "- callback_token 用于 API 回调校验\n"
                "提示：启用视频发送需配置 video_api_base_url\n"
            )
        return (
            "未知子菜单。可用：parse / login / task / file / api / net / config\n"
            "例如：/bili_menu parse"
        )

    async def _safe_send_text(self, event: AstrMessageEvent, text: str):
        chunk_size = 200
        if not text:
            return
        parts: list[str] = []
        buf = ""
        for line in text.splitlines():
            candidate = f"{buf}\n{line}" if buf else line
            if len(candidate) > chunk_size:
                if buf:
                    parts.append(buf)
                buf = line
            else:
                buf = candidate
        if buf:
            parts.append(buf)
        for part in parts:
            try:
                await event.send(MessageChain().message(part))
            except Exception as exc:
                if self._debug():
                    logger.error("bili: send failed: %s", exc)
                return None
        return None

    async def _send_plain_once(self, event: AstrMessageEvent, text: str):
        if not text:
            return
        try:
            await event.send(MessageChain().message(text))
        except Exception as exc:
            if self._debug():
                logger.error("bili: send failed: %s", exc)
        return None

    async def _send_forward_blocks(self, event: AstrMessageEvent, blocks: list[str]):
        if not blocks:
            return
        nodes: list[Node] = []
        for block in blocks:
            if not block:
                continue
            nodes.append(Node(name="BiliWatch", uin="0", content=[Plain(block)]))
        if not nodes:
            return
        chain = MessageChain()
        chain.chain.append(Nodes(nodes))
        try:
            await event.send(chain)
        except Exception as exc:
            if self._debug():
                logger.error("bili: forward send failed: %s", exc)
            return None

    def _task_status_label(self, status: str, progress: Any = None) -> str:
        status = str(status or "").lower()
        if status in {"completed", "done", "success", "finished"}:
            return "✅ 已完成"
        if status in {"running", "downloading", "processing", "active"}:
            pct = ""
            try:
                p = int(progress)
                if p >= 0:
                    pct = f" {p}%"
            except Exception:
                pct = ""
            return f"⬇️ 下载中{pct}".strip()
        if status in {"canceled", "cancelled", "stopped"}:
            return "🟨 已取消"
        if status in {"failed", "error"}:
            return "❌ 失败"
        if status in {"pending", "queued", "waiting"}:
            return "⏳ 排队中"
        return "❓ 未知"

    def _normalize_task_status(self, label: str) -> str:
        label = (label or "").strip().lower()
        if label in {"✅", "完成", "已完成", "done", "completed", "success", "finished"}:
            return "completed"
        if label in {"⬇️", "下载中", "下载", "running", "downloading", "processing", "active"}:
            return "running"
        if label in {"🟨", "已取消", "取消", "canceled", "cancelled", "stopped"}:
            return "canceled"
        if label in {"❌", "失败", "error", "failed"}:
            return "failed"
        if label in {"⏳", "排队", "排队中", "pending", "queued", "waiting"}:
            return "pending"
        if label in {"❓", "未知", "unknown"}:
            return "unknown"
        return ""

    def _file_status_label(self, name: str, size: int | None = None) -> str:
        size_text = ""
        if isinstance(size, int) and size >= 0:
            mb = size / 1024 / 1024
            size_text = f"{mb:.2f}MB"
        label = "✅ 已完成"
        if size_text:
            return f"{label} | {size_text}"
        return label

    def _normalize_file_label(self, label: str) -> str:
        label = (label or "").strip().lower()
        if label in {"✅", "完成", "已完成", "done", "completed", "success", "finished"}:
            return "completed"
        return ""

    @filter.command("bili")
    async def bili_help_cmd(self, event: AstrMessageEvent, args: str = ""):
        content = (args or "").strip().lower()
        if not content or content in {"help", "menu"}:
            return await self._send_plain_once(event, self._menu_text())
        if content in {"parse", "login", "task", "file", "api", "net", "config", "解析", "登录", "任务", "文件", "接口", "网络", "配置"}:
            return await self._send_plain_once(event, self._submenu_text(content))
        return event.plain_result("自动解析开着呢，直接丢链接就好。")

    @filter.command("bili_debug")
    async def bili_debug_cmd(self, event: AstrMessageEvent):
        payload = (self._last_json_payload or "").strip()
        if not payload:
            return event.plain_result("未捕获到 JSON 内容。")
        if len(payload) > 1500:
            payload = payload[:1500] + "...(已截断)"
        return event.plain_result(f"最近一次 JSON 卡片内容：\n{payload}")

    @filter.command("bili_api")
    async def bili_api_cmd(self, event: AstrMessageEvent, action: str = "", base_url: str = "", token: str = ""):
        action = (action or "").strip().lower()
        if action in {"show", "list", ""}:
            url = self._api_base() or "(未设置)"
            tok = self._api_token()
            tok_show = tok[:4] + "****" + tok[-4:] if tok and len(tok) > 8 else (tok or "(未设置)")
            return event.plain_result(f"当前 API:\n- base_url: {url}\n- token: {tok_show}")
        if action in {"set"}:
            if not base_url:
                return event.plain_result("用法：/bili_api set <base_url> [token]")
            ok_url = self._set_config_value("video_api_base_url", base_url.strip())
            ok_token = True
            if token:
                ok_token = self._set_config_value("video_api_login_token", token.strip())
            if not (ok_url and ok_token):
                return event.plain_result("API 配置未能写入，请用控制台配置页修改。")
            # 同步回调（如果启用）
            if self._enable_http_callback() and self._api_token():
                callback_url = f"http://{self._callback_host()}:{self._callback_port()}{self._callback_path()}"
                ok, msg = await self._set_remote_callback(base_url.strip(), callback_url, self._callback_token())
                if ok:
                    return event.plain_result("API 配置已更新，并已同步回调。")
                return event.plain_result(f"API 配置已更新，但回调同步失败：{msg}")
            return event.plain_result("API 配置已更新。")
        return event.plain_result("用法：/bili_api show | /bili_api set <base_url> [token]")

    @filter.command("bili_net")
    async def bili_net_cmd(self, event: AstrMessageEvent):
        lines = ["BiliWatch 连通性测试"]

        # 官方接口
        status, data = await self._request_json(API_VIEW, {"bvid": "BV1Q541167Qg"})
        if status == 200 and isinstance(data, dict) and data.get("code") == 0:
            lines.append("官方接口：✅ 正常")
        else:
            lines.append(f"官方接口：❌ 异常（HTTP {status}）")

        # 自建 API
        base_url = self._api_base()
        if base_url:
            s2, _ = await self._request_text(f"{base_url}/api", {})
            if s2 == 200:
                lines.append("自建 API：✅ 正常")
            else:
                lines.append(f"自建 API：❌ 异常（HTTP {s2}）")
        else:
            lines.append("自建 API：⚠️ 未配置")

        # 回调监听
        if self._enable_http_callback():
            callback_url = f"http://{self._callback_host()}:{self._callback_port()}{self._callback_path()}"
            headers = {}
            token = self._callback_token()
            if token:
                headers["X-Callback-Token"] = token
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(3)) as client:
                    resp = await client.post(callback_url, json={"status": "test"}, headers=headers)
                if resp.status_code == 200:
                    lines.append("回调监听：✅ 可达")
                else:
                    lines.append(f"回调监听：❌ 异常（HTTP {resp.status_code}）")
            except Exception as exc:
                if self._debug():
                    logger.error("bili: callback test failed: %s", exc)
                lines.append("回调监听：❌ 不可达")
        else:
            lines.append("回调监听：⚠️ 未启用")

        return await self._send_plain_once(event, "\n".join(lines))

    @filter.command("bili_callback")
    async def bili_callback_cmd(self, event: AstrMessageEvent, action: str = "", token: str = ""):
        action = (action or "").strip().lower()
        if action == "set":
            if not token:
                return event.plain_result("用法：/bili_callback set <token>")
            ok = self._set_config_value("callback_token", token.strip())
            if not ok:
                return event.plain_result("回调 token 写入失败，请用配置页修改。")
            return event.plain_result("回调 token 已更新，请执行 /bili_callback sync 同步到 API。")
        if action != "sync":
            return event.plain_result("用法：/bili_callback sync | /bili_callback set <token>")
        base_url = self._api_base()
        if not base_url:
            return event.plain_result("先配 video_api_base_url。")
        token_api = self._api_token()
        if not token_api:
            return event.plain_result("先配 video_api_login_token。")
        callback_url = f"http://{self._callback_host()}:{self._callback_port()}{self._callback_path()}"
        callback_token = self._callback_token()
        ok, msg = await self._set_remote_callback(base_url, callback_url, callback_token)
        return event.plain_result(msg if ok else msg)


    @filter.regex(r".*(b23\.tv/|bilibili\.com/video/|BV[0-9A-Za-z]{10}|av\d+|bilibili://video/).*")
    async def bili_auto(self, event: AstrMessageEvent):
        if not self._enable_auto():
            return
        text = self._get_event_text(event)
        if self._looks_like_command(text):
            return
        result = await self._handle_text(event, text)
        return result


class Main(BilibiliWatch):
    """兼容旧版加载器"""

