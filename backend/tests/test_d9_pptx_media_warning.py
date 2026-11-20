"""D9 闸门 — PPTX 嵌入音视频不得静默丢失（仅告警，不提取）。

红基线契约：嵌入媒体零检测，内容丢失且无感知。修复后契约：
    - 按 slide part 关系（video/audio/media reltype）计数，同目标去重；
    - last_parse_warnings 记「含 N 个嵌入音视频，未解析」；
    - 纯图文 PPTX 无 warning（正向证人）；不引入转换依赖、不提取媒体。

真实边界：python-pptx add_movie 真实嵌入媒体 part（mp4 字节为占位，
解析器只读关系结构不解码）；audio 经 OOXML reltype/videoFile 改写构造。
"""

from __future__ import annotations

import base64
import io
import zipfile

import pytest


@pytest.fixture(autouse=True)
def _strip_no_proxy(monkeypatch):
    """剥离本机 NO_PROXY 裸 ::1（B10 教训：httpx trust_env 解析即炸）。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


_FAKE_MP4 = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 64
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_MARKER = "MEDIA-BYTES-MARKER-不应出现在解析产物"


def _make_pptx(tmp_path, movies=0, picture=False, name="d9.pptx"):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tx = slide.shapes.add_textbox(Inches(1), Inches(0.2), Inches(6), Inches(0.5))
    tx.text_frame.text = "幻灯片正文内容"
    for i in range(movies):
        # 字节互异 — 相同字节会被 python-pptx 合并为同一媒体 part
        slide.shapes.add_movie(
            io.BytesIO(_FAKE_MP4 + bytes([i + 1]) * 16),
            Inches(1 + i),
            Inches(1),
            Inches(2),
            Inches(1.5),
            mime_type="video/mp4",
        )
    if picture:
        slide.shapes.add_picture(
            io.BytesIO(_PNG_1PX), Inches(1), Inches(3), Inches(1), Inches(1)
        )
    path = tmp_path / name
    prs.save(str(path))
    return str(path)


def _rewrite_to_audio(path):
    """把 video reltype/videoFile 改写为 audio — 构造真实 OOXML 音频形状。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(buf, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith(".rels"):
                data = data.replace(b"relationships/video", b"relationships/audio")
            elif item.filename.startswith(
                "ppt/slides/slide"
            ) and item.filename.endswith(".xml"):
                data = data.replace(b"videoFile", b"audioFile")
            zout.writestr(item, data)
    with open(path, "wb") as f:
        f.write(buf.getvalue())


class TestD9PptxMediaWarning:
    async def test_video_warning_count(self, tmp_path):
        from app.document.pptx_parser import PPTXParser

        path = _make_pptx(tmp_path, movies=2)
        parser = PPTXParser()
        out = await parser.parse(path)
        assert "幻灯片正文内容" in out
        assert any("含 2 个嵌入音视频，未解析" in w for w in parser.last_parse_warnings)

    async def test_single_video_dedup(self, tmp_path):
        """同一媒体目标的 video+media 关系不得重复计数。"""
        from app.document.pptx_parser import PPTXParser

        path = _make_pptx(tmp_path, movies=1)
        parser = PPTXParser()
        await parser.parse(path)
        assert any("含 1 个嵌入音视频，未解析" in w for w in parser.last_parse_warnings)

    async def test_audio_warning(self, tmp_path):
        from app.document.pptx_parser import PPTXParser

        path = _make_pptx(tmp_path, movies=1)
        _rewrite_to_audio(path)
        parser = PPTXParser()
        await parser.parse(path)
        assert any("含 1 个嵌入音视频，未解析" in w for w in parser.last_parse_warnings)

    async def test_plain_pptx_no_warning_witness(self, tmp_path):
        """正向证人：纯图文 PPTX 不产媒体 warning，正文完整。"""
        from app.document.pptx_parser import PPTXParser

        path = _make_pptx(tmp_path, movies=0, picture=True)
        parser = PPTXParser()
        out = await parser.parse(path)
        assert "幻灯片正文内容" in out
        assert not any("嵌入音视频" in w for w in parser.last_parse_warnings)

    async def test_media_bytes_not_extracted(self, tmp_path):
        """仅告警不提取：媒体字节不得进入解析产物。"""
        from app.document.pptx_parser import PPTXParser

        path = _make_pptx(tmp_path, movies=1)
        out = await PPTXParser().parse(path)
        assert _MARKER not in out
        assert "ftypisom" not in out

    async def test_warnings_reset_between_parses(self, tmp_path):
        """同一解析器复用：媒体 warning 不得残留到下一份纯图文文档。"""
        from app.document.pptx_parser import PPTXParser

        media_path = _make_pptx(tmp_path, movies=1, name="media.pptx")
        plain_path = _make_pptx(tmp_path, movies=0, name="plain.pptx")
        parser = PPTXParser()
        await parser.parse(media_path)
        assert any("嵌入音视频" in w for w in parser.last_parse_warnings)
        await parser.parse(plain_path)
        assert not any("嵌入音视频" in w for w in parser.last_parse_warnings)
