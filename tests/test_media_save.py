"""测试 proxy.media 的多模态文件保存子系统。

覆盖：
- _ext_for_mime：MIME 类型 → 扩展名推断
- _extract_base64_data：从多种 content 块提取 base64 数据与扩展名
- _save_multimodal_files：按 save_images / save_audios / save_files 开关保存文件
- _write_media_file：同步原子写文件
"""

import base64

import pytest

from models.api_types import APIType
from models.channel import Channel, Endpoint
from proxy import media as proxy_media
from proxy.media import (
    _ext_for_mime,
    _extract_base64_data,
    _save_multimodal_files,
    _write_media_file,
)


def _channel(name="media-test"):
    return Channel(
        id="ch_media",
        name=name,
        endpoints=[Endpoint(api_type=APIType.OPENAI_CHAT, base_url="https://example.com")],
        api_key="key",
        models=["gpt-4o"],
    )


class TestExtForMime:
    def test_common_mime_types(self):
        assert _ext_for_mime("image/png") == "png"
        assert _ext_for_mime("image/jpeg") == "jpg"
        assert _ext_for_mime("audio/wav") == "wav"
        assert _ext_for_mime("application/pdf") == "pdf"

    def test_strips_parameters(self):
        assert _ext_for_mime("image/png;base64") == "png"
        assert _ext_for_mime("image/jpeg; charset=utf-8") == "jpg"

    def test_unknown_mime_returns_empty_or_bin(self):
        # mimetypes 对未知类型的返回依赖平台数据库，只断言成功率或为空
        ext = _ext_for_mime("application/octet-stream")
        assert ext in ("", "bin")
        assert _ext_for_mime("") == ""


class TestExtractBase64Data:
    def test_openai_image_url_data_url(self):
        b64 = base64.b64encode(b"fake-image-bytes").decode()
        part = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        data, ext = _extract_base64_data(part)
        assert data == b"fake-image-bytes"
        assert ext == "png"

    def test_image_url_not_data_url_returns_none(self):
        part = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
        assert _extract_base64_data(part) is None

    def test_anthropic_image_base64(self):
        b64 = base64.b64encode(b"anthropic-image").decode()
        part = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        }
        data, ext = _extract_base64_data(part)
        assert data == b"anthropic-image"
        assert ext == "jpg"

    def test_anthropic_image_default_mime(self):
        b64 = base64.b64encode(b"x").decode()
        part = {"type": "image", "source": {"type": "base64", "data": b64}}
        data, ext = _extract_base64_data(part)
        assert ext == "png"

    def test_input_audio(self):
        b64 = base64.b64encode(b"the-audio").decode()
        part = {"type": "input_audio", "input_audio": {"format": "mp3", "data": b64}}
        data, ext = _extract_base64_data(part)
        assert data == b"the-audio"
        assert ext == "mp3"

    def test_file_with_filename(self):
        b64 = base64.b64encode(b"file-content").decode()
        part = {
            "type": "file",
            "file": {"file_data": b64, "filename": "report.pdf"},
        }
        data, ext = _extract_base64_data(part)
        assert data == b"file-content"
        assert ext == "pdf"

    def test_file_without_extension_returns_bin(self):
        b64 = base64.b64encode(b"file-content").decode()
        part = {"type": "file", "file": {"file_data": b64}}
        data, ext = _extract_base64_data(part)
        assert ext == "bin"

    def test_invalid_base64_returns_none(self):
        part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,@@@not-base64@@@"}}
        assert _extract_base64_data(part) is None

    def test_unsupported_type_returns_none(self):
        assert _extract_base64_data({"type": "text", "text": "hi"}) is None


class TestSaveMultimodalFiles:
    @pytest.mark.asyncio
    async def test_all_flags_off_does_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(proxy_media, "get_setting", lambda key: False)
        request_data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{base64.b64encode(b'x').decode()}"},
                        }
                    ],
                }
            ]
        }
        await _save_multimodal_files(request_data, "gpt-4o", _channel())
        assert not (tmp_path / "logs").exists()

    @pytest.mark.asyncio
    async def test_saves_image_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        flags = {"save_images": True, "save_audios": False, "save_files": False}
        monkeypatch.setattr(proxy_media, "get_setting", lambda key: flags.get(key, False))
        img_bytes = b"\x89PNG-fake-image"
        request_data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{base64.b64encode(img_bytes).decode()}"},
                        }
                    ],
                }
            ]
        }
        await _save_multimodal_files(request_data, "gpt-4o", _channel())
        images_dir = tmp_path / "logs" / "images"
        files = list(images_dir.glob("*.png"))
        assert len(files) == 1
        assert files[0].read_bytes() == img_bytes
        assert "gpt-4o" in files[0].name

    @pytest.mark.asyncio
    async def test_audio_and_file_respect_their_own_flags(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        flags = {"save_images": False, "save_audios": True, "save_files": True}
        monkeypatch.setattr(proxy_media, "get_setting", lambda key: flags.get(key, False))
        request_data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "format": "wav",
                                "data": base64.b64encode(b"audio").decode(),
                            },
                        },
                        {
                            "type": "file",
                            "file": {
                                "file_data": base64.b64encode(b"doc").decode(),
                                "filename": "note.txt",
                            },
                        },
                    ],
                }
            ]
        }
        await _save_multimodal_files(request_data, "deepseek-v3", _channel())
        audios = list((tmp_path / "logs" / "audios").glob("*"))
        files = list((tmp_path / "logs" / "files").glob("*"))
        assert any(a.suffix == ".wav" for a in audios)
        assert any(f.suffix == ".txt" for f in files)

    @pytest.mark.asyncio
    async def test_sanitizes_model_name_in_filename(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        flags = {"save_images": True, "save_audios": False, "save_files": False}
        monkeypatch.setattr(proxy_media, "get_setting", lambda key: flags.get(key, False))
        request_data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{base64.b64encode(b'x').decode()}"},
                        }
                    ],
                }
            ]
        }
        await _save_multimodal_files(request_data, "my/vendor:model", _channel())
        names = [p.name for p in (tmp_path / "logs" / "images").glob("*.png")]
        assert names and "my_vendor_model" in names[0]
        assert "/" not in names[0] and ":" not in names[0]

    @pytest.mark.asyncio
    async def test_non_list_messages_returns_early(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        flags = {"save_images": True, "save_audios": False, "save_files": False}
        monkeypatch.setattr(proxy_media, "get_setting", lambda key: flags.get(key, False))
        await _save_multimodal_files({"messages": "not-a-list"}, "m", _channel())
        assert not (tmp_path / "logs").exists()


class TestWriteMediaFile:
    def test_writes_file_atomically(self, tmp_path):
        file_dir = tmp_path / "logs" / "images"
        file_path = file_dir / "test.png"
        _write_media_file(file_dir, file_path, b"data")
        assert file_path.read_bytes() == b"data"
        assert not file_path.with_suffix(".png.tmp").exists()

    def test_creates_parent_directory(self, tmp_path):
        file_dir = tmp_path / "a" / "b" / "c"
        file_path = file_dir / "f.bin"
        _write_media_file(file_dir, file_path, b"x")
        assert file_path.read_bytes() == b"x"
