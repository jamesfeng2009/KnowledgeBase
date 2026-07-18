"""
图片对象存储工具 — 单一职责：图片过滤、格式校验、上传 MinIO。

对齐 PDF→Markdown 流程图中的图片处理分支：
    1. 小图过滤（宽或高 < min_size 像素）— 剔除图标、装饰性小图；
    2. 格式识别（JPEG / PNG / WebP）— 不支持的格式跳过；
    3. 上传对象存储 — MinIO upload_file 返回 URL；
    4. 整批失败降级 — 记录告警日志，调用方降级为纯文本或 VLM 描述。

使用方式::

    from app.document.image_storage import upload_image

    url = await upload_image(
        image_bytes=img_bytes,
        ext="png",
        width=800,
        height=600,
        doc_id="doc-uuid",
        page=3,
        idx=0,
        min_size=50,
    )
    if url:
        # 上传成功，URL 可用于 ![](url) 或 <img src="url"/>
        ...

设计要点：
    - 零外部图片库依赖 — 用 struct 手动解析 PNG/JPEG/WebP 头部获取尺寸；
    - 优雅降级 — MinIO 不可用时返回 None，调用方走 VLM 描述 fallback；
    - 对象命名规范 — {doc_id}/page{page}_img{idx}.{ext}，便于按文档/页码检索。
"""

from __future__ import annotations

import struct
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)

#: 支持的图片格式（MIME type → 扩展名映射）
_SUPPORTED_FORMATS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def get_image_dimensions(
    image_bytes: bytes,
    ext: str = "",
) -> tuple[int, int]:
    """从图片字节流解析宽高 — 零外部依赖。

    支持 PNG / JPEG / WebP 三种格式。解析失败返回 (0, 0)。

    Args:
        image_bytes: 图片二进制数据。
        ext: 扩展名提示（png/jpg/jpeg/webp），为空时自动检测。

    Returns:
        (width, height) 元组。解析失败返回 (0, 0)。
    """
    if not image_bytes or len(image_bytes) < 12:
        return 0, 0

    ext = ext.lower().lstrip(".")
    # 标准化扩展名
    if ext == "jpg":
        ext = "jpeg"
    if ext == "jfif":
        ext = "jpeg"

    # 自动检测格式
    if not ext:
        if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        elif image_bytes[:2] == b"\xff\xd8":
            ext = "jpeg"
        elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            ext = "webp"
        else:
            return 0, 0

    if ext == "png":
        return _parse_png_dimensions(image_bytes)
    if ext == "jpeg":
        return _parse_jpeg_dimensions(image_bytes)
    if ext == "webp":
        return _parse_webp_dimensions(image_bytes)
    return 0, 0


def _parse_png_dimensions(data: bytes) -> tuple[int, int]:
    """解析 PNG 图片宽高 — IHDR 块固定在偏移 16 处。"""
    try:
        # PNG: 8 字节签名 + IHDR 块
        # IHDR: 4 字节长度 + 4 字节类型 + 4 字节宽 + 4 字节高
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    except (struct.error, IndexError):
        return 0, 0


def _parse_jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """解析 JPEG 图片宽高 — 扫描 SOF0/SOF1/SOF2 标记。

    JPEG 由多个段组成，SOF 段包含图片高宽信息。
    需要逐段扫描跳过非 SOF 段。
    """
    try:
        idx = 2  # 跳过 SOI 标记 (0xFFD8)
        while idx < len(data) - 1:
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            # SOF0 (0xC0) ~ SOF15 (0xCF)，排除 SOF4(0xC4)/SOF8(0xC8)/SOF12(0xCC)
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                # SOF 段: 2 字节长度 + 1 字节精度 + 2 字节高 + 2 字节宽
                height, width = struct.unpack(
                    ">HH", data[idx + 5 : idx + 9]
                )
                return width, height
            # 跳过非 SOF 段: 2 字节长度 + 数据
            if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD9):
                idx += 2
            elif marker == 0xDA:  # SOS — 后面是图像数据
                break
            else:
                seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
                idx += 2 + seg_len
        return 0, 0
    except (struct.error, IndexError):
        return 0, 0


def _parse_webp_dimensions(data: bytes) -> tuple[int, int]:
    """解析 WebP 图片宽高 — RIFF 容器 + VP8/VP8L/VP8X 载荷。"""
    try:
        if len(data) < 30:
            return 0, 0
        # RIFF 头: 4 字节 "RIFF" + 4 字节文件大小 + 4 字节 "WEBP"
        chunk_type = data[12:16]
        if chunk_type == b"VP8 ":
            # VP8 (有损): 10 字节 chunk header + 3 字节 frame tag + 7 字节
            width, height = struct.unpack("<HH", data[26:30])
            # 宽高按 1 位掩码修正
            return width & 0x3FFF, height & 0x3FFF
        elif chunk_type == b"VP8L":
            # VP8L (无损): 9 字节 chunk header + 1 字节 signature + 4 字节
            bits = struct.unpack("<I", data[21:25])[0]
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
        elif chunk_type == b"VP8X":
            # VP8X (扩展): canvas 宽高在偏移 24-29
            width = (struct.unpack("<I", data[24:27] + b"\x00")[0]) + 1
            height = (struct.unpack("<I", data[27:30] + b"\x00")[0]) + 1
            return width, height
        return 0, 0
    except (struct.error, IndexError):
        return 0, 0


def is_supported_format(ext: str) -> bool:
    """检查图片格式是否支持上传。

    Args:
        ext: 扩展名（png/jpg/jpeg/webp）。

    Returns:
        True 如果格式受支持。
    """
    ext = ext.lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    return ext in ("png", "jpeg", "webp")


async def upload_image(
    image_bytes: bytes,
    ext: str,
    doc_id: str,
    page: int,
    idx: int,
    min_size: int = 50,
    width: int = 0,
    height: int = 0,
) -> str | None:
    """上传单张图片到 MinIO — 含小图过滤和格式校验。

    流程：
        1. 格式校验 — 仅支持 JPEG/PNG/WebP；
        2. 尺寸解析 — 未传入宽高时自动解析；
        3. 小图过滤 — 宽或高 < min_size 时跳过返回 None；
        4. 上传 MinIO — 对象命名 {doc_id}/page{page}_img{idx}.{ext}；
        5. 返回 minio:// URL。

    Args:
        image_bytes: 图片二进制数据。
        ext: 扩展名（png/jpg/jpeg/webp）。
        doc_id: 文档 ID（用于对象命名空间隔离）。
        page: 页码（用于对象命名）。
        idx: 图片在当前页的序号（用于对象命名）。
        min_size: 最小尺寸阈值，宽或高小于此值跳过。
        width: 已知宽度（0 表示未知，自动解析）。
        height: 已知高度（0 表示未知，自动解析）。

    Returns:
        上传成功返回 ``minio://{bucket}/{object_name}`` URL。
        格式不支持、小图过滤、上传失败时返回 None。
    """
    if not image_bytes:
        return None

    # 1. 格式校验
    ext = ext.lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    if ext not in ("png", "jpeg", "webp"):
        log.debug("image_upload.unsupported_format", ext=ext)
        return None

    # 2. 尺寸解析
    if width == 0 or height == 0:
        width, height = get_image_dimensions(image_bytes, ext)

    # 3. 小图过滤
    if min_size > 0 and (width < min_size or height < min_size):
        log.debug(
            "image_upload.filtered_small",
            width=width,
            height=height,
            min_size=min_size,
            page=page,
        )
        return None

    # 4. 上传 MinIO
    settings = get_settings()
    bucket = settings.MINIO_BUCKET
    object_name = f"{doc_id}/page{page}_img{idx}.{ext}"
    content_type = f"image/{ext}"

    try:
        from app.utils.minio_client import upload_file

        url = await upload_file(
            bucket=bucket,
            object_name=object_name,
            data=image_bytes,
            content_type=content_type,
        )
        log.debug(
            "image_upload.success",
            object_name=object_name,
            width=width,
            height=height,
            page=page,
        )
        return url
    except ImportError:
        log.warning("image_upload.minio_not_installed")
        return None
    except Exception as exc:
        log.warning("image_upload.failed", object_name=object_name, error=str(exc))
        return None


async def upload_images_batch(
    images: list[dict[str, Any]],
    doc_id: str,
    min_size: int = 50,
) -> list[str | None]:
    """批量上传图片 — 单张失败不影响其他图片。

    对齐图片流程中的容错设计：
        - 整批失败（MinIO 不可用）→ 记录告警，返回全 None；
        - 单张失败 → 跳过该张，继续处理。

    Args:
        images: 图片信息列表，每项包含:
            - bytes: 图片二进制
            - ext: 扩展名
            - page: 页码
            - idx: 序号
            - width (optional): 已知宽度
            - height (optional): 已知高度
        doc_id: 文档 ID。
        min_size: 最小尺寸阈值。

    Returns:
        URL 列表（与输入列表等长，上传失败的项为 None）。
    """
    if not images:
        return []

    results: list[str | None] = []
    failed_count = 0

    for img_info in images:
        url = await upload_image(
            image_bytes=img_info.get("bytes", b""),
            ext=img_info.get("ext", ""),
            doc_id=doc_id,
            page=img_info.get("page", 0),
            idx=img_info.get("idx", 0),
            min_size=min_size,
            width=img_info.get("width", 0),
            height=img_info.get("height", 0),
        )
        results.append(url)
        if url is None:
            failed_count += 1

    if failed_count == len(images) and len(images) > 0:
        log.warning(
            "image_upload.batch_all_failed",
            total=len(images),
            doc_id=doc_id,
        )

    return results
