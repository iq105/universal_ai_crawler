"""验证码自动识别服务（ddddocr 本地 OCR，可选打码平台）

降级链：本地 ddddocr 识别 → 失败/未安装 → 交回人工介入。
支持文字验证码（含简单滑块缺口识别）。
"""
import base64
import logging

from app.config import settings

logger = logging.getLogger(__name__)

try:
    import ddddocr
    _ocr = ddddocr.DdddOcr(show_ad=False)
    _OCR_OK = True
except Exception as exc:  # noqa: BLE001
    logger.warning("ddddocr 文字验证码 OCR 加载失败，将直接交人工：%s", exc)
    _OCR_OK = False
    _ocr = None

try:
    _slide = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
    _SLIDE_OK = True
except Exception as exc:  # noqa: BLE001
    logger.warning("ddddocr 滑块缺口识别加载失败：%s", exc)
    _SLIDE_OK = False
    _slide = None


def _image_bytes(base64_or_path: str) -> bytes | None:
    """入参可能是 base64 数据串或本地路径，统一返回图片 bytes。"""
    if not base64_or_path:
        return None
    s = base64_or_path.strip()
    if s.startswith("data:"):  # data:image/...;base64,xxx
        s = s.split(",", 1)[-1]
    # 是有效 base64 且看起来像图像数据
    try:
        if "," not in s and (len(s) > 64 and not s.startswith("/") and "." not in s.split("/")[0]):
            return base64.b64decode(s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("base64 图像内容解码失败，改按文件路径处理：%s", exc)
    # 否则当作本地文件路径
    import os

    if os.path.isfile(s):
        with open(s, "rb") as f:
            return f.read()
    try:
        return base64.b64decode(s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("无法将输入解析为验证码图片：%s", exc)
        return None


def ocr_text(base64_or_path: str) -> dict:
    """识别文字验证码。返回 text 或错误原因（未安装/解析失败）。"""
    if not _OCR_OK:
        return {"ok": False, "source": "local", "reason": "ddddocr 未安装，请 pip install ddddocr，或交人工输入"}
    img = _image_bytes(base64_or_path)
    if not img:
        return {"ok": False, "source": "local", "reason": "无法解码验证码图片，请提供截图或交人工输入"}
    try:
        text = _ocr.classification(img)
        text = "".join(ch for ch in (text or "") if ch.strip())
        if not text:
            return {"ok": False, "source": "local", "reason": "OCR 未识别出有效字符，请交人工输入"}
        return {"ok": True, "source": "local", "text": text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "source": "local", "reason": f"OCR 识别失败：{exc}"}


def detect_slider(base64_or_path: str) -> dict:
    """检测滑块缺口（返回是否检测到）。仅当模型可用时尝试。"""
    if not _SLIDE_OK:
        return {"ok": False, "reason": "滑块识别模型不可用，请交人工完成滑块"}
    img = _image_bytes(base64_or_path)
    if not img:
        return {"ok": False, "reason": "无法解码滑块图片"}
    try:
        pos = _slide.detection(img)
        return {"ok": True, "detected": pos is not None, "x": pos[0] if pos else None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"滑块识别失败：{exc}"}