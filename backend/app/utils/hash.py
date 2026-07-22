"""
内容哈希工具 — 支撑 P1-B 增量更新与去重。

核心能力：
    1. 文档级哈希（content_hash）— 整篇文档内容的 SHA-256，用于跨知识库查重；
    2. 分块级哈希（chunk_hash）— 单个 chunk 内容的 SHA-256，用于增量更新比对；
    3. 确定性 chunk ID — uuid5(doc_id + chunk_hash + index)，
       相同内容重复处理时生成相同 ID，实现幂等写入。

遵循单一职责：仅提供哈希计算与 ID 生成，不涉及业务逻辑。
遵循幂等性约定：确定性 ID 保证同一文档重复处理时 chunk ID 不变，
    使 DB upsert 和索引覆盖写入安全可行。
"""

from __future__ import annotations

import hashlib
import uuid

# uuid5 命名空间 — 固定常量确保跨进程/跨机器生成相同 ID
_CHUNK_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def compute_content_hash(text: str) -> str:
    """计算文本内容的 SHA-256 哈希。

    用于文档级和分块级内容指纹。
    相同内容始终生成相同哈希，实现幂等比对。

    Args:
        text: 文本内容。

    Returns:
        64 字符十六进制哈希字符串。

    示例::

        >>> h = compute_content_hash("Hello, world!")
        >>> len(h)
        64
        >>> compute_content_hash("Hello, world!") == h
        True
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_file_hash(file_path: str, chunk_size: int = 8192) -> str:
    """计算文件内容的 SHA-256 哈希（流式读取，支持大文件）。

    用于文件级去重 — 相同文件内容（不同文件名）生成相同哈希。

    Args:
        file_path: 文件路径。
        chunk_size: 流式读取块大小（字节），默认 8KB。

    Returns:
        64 字符十六进制哈希字符串。

    Raises:
        FileNotFoundError: 文件不存在。
        OSError: 文件读取错误。
    """
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            hasher.update(data)
    return hasher.hexdigest()


def generate_deterministic_chunk_id(
    doc_id: str,
    content_hash: str,
    index: int,
) -> str:
    """生成确定性 chunk ID — uuid5(doc_id + content_hash + index)。

    相同文档重复处理时，相同位置的相同内容生成相同 ID，
    使 DB upsert 和索引覆盖写入安全可行（幂等性保证）。

    Args:
        doc_id: 文档 ID（字符串形式）。
        content_hash: chunk 内容的 SHA-256 哈希。
        index: chunk 在文档中的序号（从 0 开始）。

    Returns:
        UUID 字符串（36 字符，含连字符）。

    示例::

        >>> id1 = generate_deterministic_chunk_id("doc-1", "abc123", 0)
        >>> id2 = generate_deterministic_chunk_id("doc-1", "abc123", 0)
        >>> id1 == id2
        True
        >>> id3 = generate_deterministic_chunk_id("doc-1", "abc123", 1)
        >>> id1 != id3
        True
    """
    # 组合键：doc_id + content_hash + index
    key = f"{doc_id}:{content_hash}:{index}"
    return str(uuid.uuid5(_CHUNK_NAMESPACE, key))


def compute_chunk_hash_with_metadata(
    content: str,
    title_path: str = "",
    content_type: str = "",
) -> str:
    """计算包含元数据的 chunk 哈希。

    将 content + title_path + content_type 一起哈希，
    确保元数据变化也能被增量更新检测到。

    Args:
        content: chunk 文本内容。
        title_path: 标题路径锚点。
        content_type: 内容类型标签。

    Returns:
        64 字符十六进制哈希字符串。
    """
    combined = f"{content}\x00{title_path}\x00{content_type}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def find_duplicate_by_hash(
    content_hash: str,
    existing_hashes: dict[str, str],
) -> str | None:
    """在已有哈希集合中查找重复内容。

    用于上传查重 — 检查新文档内容是否与已有文档重复。

    Args:
        content_hash: 新文档的内容哈希。
        existing_hashes: {doc_id: content_hash} 映射。

    Returns:
        重复文档的 doc_id，无重复返回 None。
    """
    for doc_id, hash_val in existing_hashes.items():
        if hash_val == content_hash:
            return doc_id
    return None
