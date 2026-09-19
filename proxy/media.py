"""
多模态文件保存：MIME→扩展名推断、base64 提取、按目录落盘。

真实实现（从 proxy.core 迁入）；proxy.routing 对本模块符号做门面聚合。
"""

import asyncio
import base64
import hashlib
import mimetypes
import os
from datetime import datetime
from pathlib import Path

from loguru import logger

from config import get_setting
from models.channel import Channel

_MIME_FALLBACK = {
    "audio/wav": "wav",
}


def _decode_base64(value: object) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        return None


def _safe_extension(value: object, fallback: str) -> str:
    """Keep a filename suffix as a suffix, never as a path fragment."""
    if not isinstance(value, str):
        return fallback
    extension = value.strip().lower().lstrip(".")
    return extension if 0 < len(extension) <= 16 and extension.isalnum() else fallback


def _ext_for_mime(mime_type: str) -> str:
    """根据 MIME 类型推断扩展名，去掉前导点。"""
    clean_mime = mime_type.split(";")[0].strip()
    if not clean_mime:
        return ""
    ext = mimetypes.guess_extension(clean_mime) or _MIME_FALLBACK.get(clean_mime, "")
    return ext.lstrip(".")


def _extract_base64_data(part: dict) -> tuple[bytes, str] | None:
    """
    从多模态 content 块中提取 base64 数据与扩展名。

    支持：
    - OpenAI Chat image_url (data URL)
    - Anthropic image (source.base64)
    - OpenAI input_audio
    - OpenAI file (file.file_data)
    """
    part_type = part.get("type", "")

    # OpenAI Chat / Responses image_url
    if part_type == "image_url":
        image_url = part.get("image_url", {})
        if isinstance(image_url, dict):
            url = image_url.get("url", "")
            if isinstance(url, str) and url.startswith("data:"):
                header, _, b64 = url.partition(",")
                mime = "image/png"
                if ";" in header and ":" in header:
                    mime = header.split(";")[0].split(":", 1)[1]
                decoded = _decode_base64(b64)
                return (decoded, _safe_extension(_ext_for_mime(mime), "png")) if decoded is not None else None

    # Anthropic image
    if part_type == "image":
        source = part.get("source", {})
        if isinstance(source, dict) and source.get("type") == "base64":
            mime = source.get("media_type", "image/png")
            data = source.get("data", "")
            decoded = _decode_base64(data)
            return (decoded, _safe_extension(_ext_for_mime(mime), "png")) if decoded is not None else None

    # OpenAI input_audio
    if part_type == "input_audio":
        audio = part.get("input_audio", {})
        if isinstance(audio, dict):
            fmt = _safe_extension(audio.get("format"), "wav")
            data = audio.get("data", "")
            decoded = _decode_base64(data)
            return (decoded, fmt) if decoded is not None else None

    # OpenAI file (file_data base64)
    if part_type == "file":
        file_info = part.get("file", {})
        if isinstance(file_info, dict):
            file_data = file_info.get("file_data")
            filename = file_info.get("filename", "file")
            if file_data:
                ext = ""
                if isinstance(filename, str):
                    ext = os.path.splitext(filename)[1].lstrip(".")
                decoded = _decode_base64(file_data)
                return (decoded, _safe_extension(ext, "bin")) if decoded is not None else None

    return None


async def _save_multimodal_files(request_data: dict, model_name: str, channel: Channel) -> None:
    """
    将请求中的多模态文件保存到 logs/{images,audios,files}/ 目录。

    保存行为由 settings 中的 save_images / save_audios / save_files 控制。
    写入失败会记录 warning 日志，不会阻断请求。
    """
    save_files = bool(get_setting("save_files"))
    save_images = bool(get_setting("save_images"))
    save_audios = bool(get_setting("save_audios"))
    if not (save_files or save_images or save_audios):
        return

    messages = request_data.get("messages")
    if not isinstance(messages, list):
        return

    logs_dir = Path("logs")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved_count = {"image": 0, "audio": 0, "file": 0}

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            category: str | None = None
            if part_type in ("image_url", "image"):
                if not save_images:
                    continue
                category = "images"
            elif part_type == "input_audio":
                if not save_audios:
                    continue
                category = "audios"
            elif part_type == "file":
                if not save_files:
                    continue
                category = "files"
            else:
                continue

            extracted = _extract_base64_data(part)
            if not extracted:
                continue
            data, ext = extracted
            ext = _safe_extension(ext, {"images": "png", "audios": "wav", "files": "bin"}[category])

            file_hash = hashlib.sha256(data).hexdigest()[:8]
            safe_model = "".join(c if c.isalnum() or c in "-_" else "_" for c in (model_name or "unknown"))
            filename = f"{timestamp}_{safe_model}_{file_hash}.{ext}"
            file_dir = logs_dir / category
            resolved_dir = file_dir.resolve()
            file_path = (file_dir / filename).resolve()
            if not file_path.is_relative_to(resolved_dir):
                logger.warning("[SAVE_MEDIA] rejected media path outside target directory")
                continue

            try:
                await asyncio.to_thread(_write_media_file, file_dir, file_path, data)
                saved_count[{"images": "image", "audios": "audio", "files": "file"}[category]] += 1
            except Exception as e:
                logger.warning(f"[SAVE_MEDIA] 保存多模态文件失败: {file_path}: {e}")

    total = sum(saved_count.values())
    if total:
        logger.info(f"[SAVE_MEDIA] 已保存 {total} 个多模态文件: {saved_count} 渠道={channel.name} 模型={model_name}")


def _write_media_file(file_dir: Path, file_path: Path, data: bytes) -> None:
    """同步写入文件，供 asyncio.to_thread 调用。"""
    file_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


save_multimodal_files = _save_multimodal_files

__all__ = [
    "save_multimodal_files",
    "_extract_base64_data",
    "_ext_for_mime",
    "_save_multimodal_files",
    "_write_media_file",
]
