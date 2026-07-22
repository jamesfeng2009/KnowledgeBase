"""
P1-B 内容哈希 + 确定性 chunk ID 测试。

测试覆盖：
    1. compute_content_hash — SHA-256 文本哈希
    2. compute_file_hash — 文件哈希
    3. generate_deterministic_chunk_id — uuid5 确定性 ID
    4. compute_chunk_hash_with_metadata — 含元数据的 chunk 哈希
    5. find_duplicate_by_hash — 查重
    6. Document.content_hash 模型字段
    7. Migration 文件验证
    8. Chunker 确定性 ID 集成
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

from app.utils.hash import (
    compute_chunk_hash_with_metadata,
    compute_content_hash,
    compute_file_hash,
    find_duplicate_by_hash,
    generate_deterministic_chunk_id,
)


# ======================================================================
# compute_content_hash 测试
# ======================================================================


class TestComputeContentHash:
    """SHA-256 文本哈希测试。"""

    def test_returns_64_char_hex_string(self):
        """返回 64 字符十六进制字符串。"""
        h = compute_content_hash("hello")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_input_same_hash(self):
        """相同输入生成相同哈希。"""
        h1 = compute_content_hash("Hello, world!")
        h2 = compute_content_hash("Hello, world!")
        assert h1 == h2

    def test_different_input_different_hash(self):
        """不同输入生成不同哈希。"""
        h1 = compute_content_hash("Hello")
        h2 = compute_content_hash("World")
        assert h1 != h2

    def test_empty_string_hash(self):
        """空字符串也能计算哈希。"""
        h = compute_content_hash("")
        assert len(h) == 64
        # SHA-256("") = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
        assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_unicode_content_hash(self):
        """中文字符串哈希正常计算。"""
        h = compute_content_hash("你好世界")
        assert len(h) == 64
        assert h == compute_content_hash("你好世界")

    def test_whitespace_sensitive(self):
        """空白差异产生不同哈希。"""
        h1 = compute_content_hash("hello world")
        h2 = compute_content_hash("hello  world")
        assert h1 != h2


# ======================================================================
# compute_file_hash 测试
# ======================================================================


class TestComputeFileHash:
    """文件哈希测试。"""

    def test_file_hash_consistent(self, tmp_path):
        """同一文件多次计算结果一致。"""
        f = tmp_path / "test.txt"
        f.write_text("test content")
        h1 = compute_file_hash(str(f))
        h2 = compute_file_hash(str(f))
        assert h1 == h2

    def test_different_files_different_hash(self, tmp_path):
        """不同内容文件哈希不同。"""
        f1 = tmp_path / "a.txt"
        f1.write_text("content A")
        f2 = tmp_path / "b.txt"
        f2.write_text("content B")
        assert compute_file_hash(str(f1)) != compute_file_hash(str(f2))

    def test_same_content_different_name_same_hash(self, tmp_path):
        """相同内容不同文件名哈希相同（去重基础）。"""
        f1 = tmp_path / "a.txt"
        f1.write_text("same content")
        f2 = tmp_path / "b.txt"
        f2.write_text("same content")
        assert compute_file_hash(str(f1)) == compute_file_hash(str(f2))

    def test_large_file_hash(self, tmp_path):
        """大文件哈希正常计算（流式读取）。"""
        f = tmp_path / "large.txt"
        f.write_text("x" * 100000)  # 100KB
        h = compute_file_hash(str(f))
        assert len(h) == 64

    def test_file_not_found_raises(self):
        """文件不存在时抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            compute_file_hash("/nonexistent/path/file.txt")


# ======================================================================
# generate_deterministic_chunk_id 测试
# ======================================================================


class TestGenerateDeterministicChunkId:
    """确定性 chunk ID 生成测试。"""

    def test_returns_uuid_string(self):
        """返回 UUID 字符串。"""
        cid = generate_deterministic_chunk_id("doc-1", "abc123", 0)
        # UUID 字符串格式：8-4-4-4-12
        assert len(cid) == 36
        uuid.UUID(cid)  # 不抛异常即为合法 UUID

    def test_same_inputs_same_id(self):
        """相同输入生成相同 ID（幂等性核心）。"""
        id1 = generate_deterministic_chunk_id("doc-1", "hash-abc", 0)
        id2 = generate_deterministic_chunk_id("doc-1", "hash-abc", 0)
        assert id1 == id2

    def test_different_doc_id_different_id(self):
        """不同 doc_id 生成不同 ID。"""
        id1 = generate_deterministic_chunk_id("doc-1", "hash-abc", 0)
        id2 = generate_deterministic_chunk_id("doc-2", "hash-abc", 0)
        assert id1 != id2

    def test_different_hash_different_id(self):
        """不同 content_hash 生成不同 ID。"""
        id1 = generate_deterministic_chunk_id("doc-1", "hash-a", 0)
        id2 = generate_deterministic_chunk_id("doc-1", "hash-b", 0)
        assert id1 != id2

    def test_different_index_different_id(self):
        """不同 index 生成不同 ID。"""
        id1 = generate_deterministic_chunk_id("doc-1", "hash-abc", 0)
        id2 = generate_deterministic_chunk_id("doc-1", "hash-abc", 1)
        assert id1 != id2

    def test_idempotent_across_calls(self):
        """跨调用幂等 — 核心保证。"""
        doc_id = str(uuid.uuid4())
        content_hash = compute_content_hash("test content")

        # 模拟两次独立处理同一文档
        id_run1 = generate_deterministic_chunk_id(doc_id, content_hash, 0)
        id_run2 = generate_deterministic_chunk_id(doc_id, content_hash, 0)

        assert id_run1 == id_run2


# ======================================================================
# compute_chunk_hash_with_metadata 测试
# ======================================================================


class TestComputeChunkHashWithMetadata:
    """含元数据的 chunk 哈希测试。"""

    def test_same_content_same_metadata_same_hash(self):
        """相同内容+元数据生成相同哈希。"""
        h1 = compute_chunk_hash_with_metadata("content", "title", "faq")
        h2 = compute_chunk_hash_with_metadata("content", "title", "faq")
        assert h1 == h2

    def test_different_content_different_hash(self):
        """不同内容生成不同哈希。"""
        h1 = compute_chunk_hash_with_metadata("content A", "", "")
        h2 = compute_chunk_hash_with_metadata("content B", "", "")
        assert h1 != h2

    def test_different_title_path_different_hash(self):
        """不同 title_path 生成不同哈希。"""
        h1 = compute_chunk_hash_with_metadata("content", "title A", "")
        h2 = compute_chunk_hash_with_metadata("content", "title B", "")
        assert h1 != h2

    def test_different_content_type_different_hash(self):
        """不同 content_type 生成不同哈希。"""
        h1 = compute_chunk_hash_with_metadata("content", "", "faq")
        h2 = compute_chunk_hash_with_metadata("content", "", "tutorial")
        assert h1 != h2

    def test_returns_64_char_hex(self):
        """返回 64 字符十六进制。"""
        h = compute_chunk_hash_with_metadata("c", "t", "f")
        assert len(h) == 64


# ======================================================================
# find_duplicate_by_hash 测试
# ======================================================================


class TestFindDuplicateByHash:
    """查重测试。"""

    def test_finds_duplicate(self):
        """能找到重复文档。"""
        existing = {"doc-1": "hash-abc", "doc-2": "hash-def"}
        result = find_duplicate_by_hash("hash-abc", existing)
        assert result == "doc-1"

    def test_no_duplicate_returns_none(self):
        """无重复返回 None。"""
        existing = {"doc-1": "hash-abc"}
        result = find_duplicate_by_hash("hash-xyz", existing)
        assert result is None

    def test_empty_existing_returns_none(self):
        """空映射返回 None。"""
        result = find_duplicate_by_hash("hash-abc", {})
        assert result is None

    def test_multiple_matches_returns_first(self):
        """多个匹配返回第一个找到的。"""
        existing = {"doc-1": "hash-abc", "doc-2": "hash-abc"}
        result = find_duplicate_by_hash("hash-abc", existing)
        assert result in ("doc-1", "doc-2")


# ======================================================================
# Document.content_hash 模型字段测试
# ======================================================================


class TestDocumentContentHashField:
    """Document 模型 content_hash 字段测试。"""

    def test_document_model_has_content_hash(self):
        """Document 模型有 content_hash 属性。"""
        from app.models.knowledge import Document

        assert hasattr(Document, "content_hash")

    def test_document_content_hash_column_type(self):
        """content_hash 列类型为 String(64)。"""
        from app.models.knowledge import Document

        col = Document.__table__.columns.get("content_hash")
        assert col is not None
        assert col.type.length == 64

    def test_document_content_hash_nullable(self):
        """content_hash 允许 NULL。"""
        from app.models.knowledge import Document

        col = Document.__table__.columns.get("content_hash")
        assert col is not None
        assert col.nullable is True


# ======================================================================
# Migration 文件验证
# ======================================================================


class TestContentHashMigration:
    """content_hash 迁移文件验证。"""

    def test_migration_file_exists(self):
        """迁移文件存在。"""
        alembic_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "alembic", "versions",
        )
        files = os.listdir(alembic_dir)
        match = [f for f in files if "content_hash" in f]
        assert len(match) >= 1

    def test_migration_has_correct_revision(self):
        """迁移 revision ID 正确。"""
        import importlib

        alembic_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "alembic", "versions",
        )
        files = os.listdir(alembic_dir)
        migration_file = [f for f in files if "content_hash" in f and f.endswith(".py")][0]
        module_path = migration_file[:-3]

        spec = importlib.util.spec_from_file_location(
            module_path,
            os.path.join(alembic_dir, migration_file),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.revision == "d1e2f3a4b5c6"
        assert module.down_revision == "c9d0e1f2a3b4"

    def test_migration_adds_content_hash_column(self):
        """迁移添加 content_hash 列。"""
        alembic_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "alembic", "versions",
        )
        files = os.listdir(alembic_dir)
        migration_file = [f for f in files if "content_hash" in f and f.endswith(".py")][0]
        with open(os.path.join(alembic_dir, migration_file)) as f:
            content = f.read()
        assert "content_hash" in content
        assert "add_column" in content
        assert "create_index" in content


# ======================================================================
# Chunker 确定性 ID 集成测试
# ======================================================================


class TestChunkerDeterministicId:
    """SemanticChunker 确定性 ID 集成测试。"""

    def test_chunk_with_doc_id_produces_deterministic_ids(self):
        """传入 doc_id 时 chunk ID 确定性。"""
        from app.rag.chunker import SemanticChunker

        content = "# Title\n\nParagraph one.\n\n## Section\n\nParagraph two."
        doc_id = str(uuid.uuid4())

        chunker = SemanticChunker()
        chunks1 = chunker.chunk(content, doc_type="md", doc_id=doc_id)
        chunks2 = chunker.chunk(content, doc_type="md", doc_id=doc_id)

        assert len(chunks1) == len(chunks2)
        for c1, c2 in zip(chunks1, chunks2):
            assert c1.id == c2.id

    def test_chunk_without_doc_id_produces_random_ids(self):
        """不传 doc_id 时 chunk ID 随机。"""
        from app.rag.chunker import SemanticChunker

        content = "# Title\n\nParagraph one."
        chunker = SemanticChunker()
        chunks1 = chunker.chunk(content, doc_type="md")
        chunks2 = chunker.chunk(content, doc_type="md")

        assert len(chunks1) == len(chunks2)
        # 不传 doc_id 时 ID 不同（随机 UUID）
        assert chunks1[0].id != chunks2[0].id

    def test_chunk_different_doc_id_different_ids(self):
        """不同 doc_id 生成不同 chunk ID。"""
        from app.rag.chunker import SemanticChunker

        content = "# Title\n\nParagraph one."
        chunker = SemanticChunker()
        chunks1 = chunker.chunk(content, doc_type="md", doc_id="doc-A")
        chunks2 = chunker.chunk(content, doc_type="md", doc_id="doc-B")

        assert chunks1[0].id != chunks2[0].id

    def test_chunker_imports_hash_utils(self):
        """chunker.py 导入了 hash 工具。"""
        import app.rag.chunker as mod

        source = open(mod.__file__).read()
        assert "compute_chunk_hash_with_metadata" in source
        assert "generate_deterministic_chunk_id" in source

    def test_document_tasks_passes_doc_id_to_chunker(self):
        """document_tasks.py 传入 doc_id 到 _chunk_document。"""
        import tasks.document_tasks as mod

        source = open(mod.__file__).read()
        assert "doc_id=doc_id" in source
        assert "compute_content_hash" in source
        assert "content_hash" in source


# ======================================================================
# 端到端去重场景验证
# ======================================================================


class TestDedupScenario:
    """端到端去重场景验证。"""

    def test_upload_dedup_scenario(self):
        """上传查重场景 — 相同内容不同文件名检测为重复。"""
        content = "这是一份重要的技术文档内容。"
        hash1 = compute_content_hash(content)
        hash2 = compute_content_hash(content)

        existing = {"doc-old": hash1}
        duplicate = find_duplicate_by_hash(hash2, existing)

        assert duplicate == "doc-old"

    def test_incremental_update_scenario(self):
        """增量更新场景 — 内容不变则 chunk ID 不变。"""
        from app.rag.chunker import SemanticChunker

        content = "# 文档标题\n\n第一段内容。\n\n## 小节\n\n第二段内容。"
        doc_id = str(uuid.uuid4())

        # 第一次处理
        chunker = SemanticChunker()
        chunks_run1 = chunker.chunk(content, doc_type="md", doc_id=doc_id)

        # 模拟重新处理（增量更新场景）
        chunks_run2 = chunker.chunk(content, doc_type="md", doc_id=doc_id)

        # chunk ID 不变 — 可以安全 upsert
        ids_run1 = {c.id for c in chunks_run1}
        ids_run2 = {c.id for c in chunks_run2}
        assert ids_run1 == ids_run2
