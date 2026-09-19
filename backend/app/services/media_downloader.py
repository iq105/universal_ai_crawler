"""媒体下载服务：视频/音频 下载 + 信息探测（封装 yt-dlp，可选 ffmpeg）

支持国内外主流站点（YouTube/B站/抖音/微博视频/油管及普通网页内嵌视频），
也支持直接传 m3u8 / mp4 / mp3 直链。ffmpeg 用于合并 m3u8 分片、音视频合成，
未配置时自动探测 PATH，找不到则由 yt-dlp 内建合并兜底。
"""
import asyncio
import os
import shutil
from pathlib import Path

from app.config import settings

try:
    import yt_dlp

    _YTDLP_OK = True
except Exception:  # noqa: BLE001
    _YTDLP_OK = False

_MEDIA_DIR = Path(settings.media_dir or "artifacts/media")
_FFMPEG = settings.ffmpeg_path or shutil.which("ffmpeg")


def _default_proxy() -> str | None:
    """从 PROXY_LIST 取第一个代理（最常用的本地代理场景就够了）。
    也兼容系统环境变量 PROXY / HTTPS_PROXY / HTTP_PROXY。"""
    if settings.proxy_list:
        first = settings.proxy_list.split(",")[0].strip()
        if first:
            return first
    for env_key in ("PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        v = os.environ.get(env_key, "").strip()
        if v:
            return v
    return None


_PROXY = _default_proxy()


def _fmt_duration(sec: int | float | None) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _probe(url: str) -> dict | None:
    """探测媒体信息（不下载），返回标题/时长/格式/作者等。"""
    if not _YTDLP_OK:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    if _PROXY:
        opts["proxy"] = _PROXY
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception:  # noqa: BLE001
            return None
    return {"title": info.get("title") or "", "webpage_url": info.get("webpage_url") or url,
        "uploader": info.get("uploader") or info.get("channel") or "", "duration": _fmt_duration(info.get("duration")),
        "duration_sec": info.get("duration"), "ext": info.get("ext"), "entry_count": info.get("playlist_count"),
        "formats": len(info.get("formats") or []), "average_rating": info.get("average_rating"),
        "view_count": info.get("view_count"), }


def detect_media(url: str) -> dict:
    """判断 URL 是否为可下载媒体资源，返回探测信息。

    返回 media_type=fetchable 表示 Http 直链（mp4/mp3/m3u8 等）可直接下载；
    media_type=page 表示需浏览器解析原页面；无法识别则 page。
    """
    if not _YTDLP_OK:
        return {"ok": False, "media_type": "unknown", "reason": "yt-dlp 未安装"}
    clean = (url or "").strip()
    # 直链后缀判断
    low = clean.lower().split("?")[0]
    if low.endswith((".mp4", ".m3u8", ".mp3", ".webm", ".flv", ".mov", ".m4a", ".avi")):
        return {"ok": True, "media_type": "direct", "url": clean}
    info = _probe(clean)
    if info and (info["duration_sec"] or info["formats"] or info["title"]):
        info["ok"] = True
        info["media_type"] = "fetchable"
        return info
    # 无法直接探测（可能需在浏览器中操作）→ 交回 Agent 走页面提取
    return {"ok": False, "media_type": "page", "url": clean, "reason": "未能直接识别为媒体，建议用浏览器解析页面"}


def _download(url: str, out_dir: Path) -> dict:
    if not _YTDLP_OK:
        return {"ok": False, "reason": "yt-dlp 未安装，请先 pip install yt-dlp"}
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 进度钩子：yt-dlp 下载过程中在 to_thread 线程里回调 ──
    def _fmt_bytes(n: float | None) -> str:
        if not n:
            return ""
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _progress_hook(d: dict) -> None:
        status = d.get("status", "")
        # 只推 downloading/finished，跳过其他（比如 'video_downloaded' 等中间状态）
        if status not in ("downloading", "finished"):
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        current = d.get("downloaded_bytes") or 0
        pct = round(current / total * 100, 1) if total > 0 else 0
        speed = d.get("speed") or 0
        eta = int(d.get("eta") or 0)
        ctx = None
        try:
            from app.core.event_bus import get_ctx
            ctx = get_ctx()
        except Exception:
            return  # 没在任务运行期间被调用（比如直接 import 测试），忽略
        if status == "finished":
            ctx.emit_threadsafe("task_progress", progress=100, speed="", eta=0,
                                filename=d.get("filename", ""), phase="done",
                                downloaded=_fmt_bytes(current), total=_fmt_bytes(total))
        else:
            # 节流：每 5% 或 0.5 秒推一次（避免刷屏）
            prev = _progress_hook._last  # type: ignore[attr-defined]
            import time as _t
            now = _t.time()
            if pct - prev["pct"] < 5 and now - prev["t"] < 0.5:
                return
            prev["pct"] = pct
            prev["t"] = now
            ctx.emit_threadsafe("task_progress", progress=pct, speed=_fmt_bytes(speed) + "/s", eta=eta,
                                filename=d.get("filename", ""), phase="downloading",
                                downloaded=_fmt_bytes(current), total=_fmt_bytes(total))
    _progress_hook._last = {"pct": -100, "t": 0}  # type: ignore[attr-defined]

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": str(out_dir / "%(title)s [%(id)s].%(ext)s"), "restrictfilenames": False,
        "writethumbnail": False, "progress_hooks": [_progress_hook]}
    if _PROXY:
        opts["proxy"] = _PROXY
    if _FFMPEG:
        opts["ffmpeg_location"] = _FFMPEG
    # 合并 m3u8 / 音视频（有 ffmpeg 时更稳；无则交给 yt-dlp 默认）
    opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}
        # 实际落地文件
        filepath = ydl.prepare_filename(info)
        # 合并后可能是 mp4，修正扩展名
        if not os.path.exists(filepath):
            mp4 = filepath.rsplit(".", 1)[0] + ".mp4"
            if os.path.exists(mp4):
                filepath = mp4
        rel = os.path.relpath(filepath, settings.media_dir) if settings.media_dir else filepath
    return {"ok": True, "title": info.get("title"), "downloaded_file": filepath, "relative_path": rel,
        "size_bytes": os.path.getsize(filepath) if os.path.exists(filepath) else None, }


async def probe_media(url: str) -> dict:
    """异步探测（供 Agent 工具调用），返回媒体信息。"""
    return await asyncio.to_thread(detect_media, url)


async def download_media(url: str) -> dict:
    """异步下载（供 Agent 工具调用），返回落地文件路径。"""
    return await asyncio.to_thread(_download, url, _MEDIA_DIR)
