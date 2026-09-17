#!/usr/bin/env python3
"""企业知识库 CLI（P2）— 通过 REST API 操作知识库。

用法示例::

    export EKB_BASE_URL=http://localhost:8000 EKB_API_KEY=sk-xxx

    python scripts/ekb_cli.py kb list
    python scripts/ekb_cli.py kb create --name "产品手册" --desc "产品知识"
    python scripts/ekb_cli.py search --query "退款政策" --top-k 5
    python scripts/ekb_cli.py doc upload --kb-id <id> --file ./a.pdf
    python scripts/ekb_cli.py wiki generate --kb-id <id>
    python scripts/ekb_cli.py queues

退出码：0 成功；2 参数错误；1 API 失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.cli import EkbClient, EkbClientError

BASE_URL_ENV = "EKB_BASE_URL"
API_KEY_ENV = "EKB_API_KEY"


def _client(args: argparse.Namespace) -> EkbClient:
    base_url = args.base_url or os.environ.get(BASE_URL_ENV, "")
    api_key = args.api_key or os.environ.get(API_KEY_ENV, "")
    if not base_url or not api_key:
        print(
            f"错误：请设置 {BASE_URL_ENV}/{API_KEY_ENV} 或 --base-url/--api-key",
            file=sys.stderr,
        )
        sys.exit(2)
    return EkbClient(base_url=base_url, api_key=api_key)


def _print(data) -> None:  # noqa: ANN001
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ekb", description="企业知识库 CLI")
    parser.add_argument("--base-url", help=f"API 地址（默认读 {BASE_URL_ENV}）")
    parser.add_argument("--api-key", help=f"API Key（默认读 {API_KEY_ENV}）")
    sub = parser.add_subparsers(dest="command", required=True)

    kb = sub.add_parser("kb", help="知识库管理")
    kb_sub = kb.add_subparsers(dest="kb_action", required=True)
    kb_sub.add_parser("list", help="列出知识库")
    kb_create = kb_sub.add_parser("create", help="创建知识库")
    kb_create.add_argument("--name", required=True)
    kb_create.add_argument("--desc", default="")

    doc = sub.add_parser("doc", help="文档操作")
    doc_sub = doc.add_subparsers(dest="doc_action", required=True)
    up = doc_sub.add_parser("upload", help="上传文档")
    up.add_argument("--kb-id", required=True)
    up.add_argument("--file", required=True)
    up.add_argument("--title", default=None)

    search = sub.add_parser("search", help="全局检索")
    search.add_argument("--query", required=True)
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--kb-id", default=None)

    wiki = sub.add_parser("wiki", help="Wiki 模式")
    wiki_sub = wiki.add_subparsers(dest="wiki_action", required=True)
    wg = wiki_sub.add_parser("generate", help="生成 Wiki")
    wg.add_argument("--kb-id", required=True)

    sub.add_parser("queues", help="任务队列面板")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = _client(args)
    try:
        if args.command == "kb":
            if args.kb_action == "list":
                _print(client.list_knowledge_bases())
            elif args.kb_action == "create":
                _print(client.create_knowledge_base(args.name, args.desc))
        elif args.command == "doc" and args.doc_action == "upload":
            _print(client.upload_document(args.kb_id, args.file, args.title))
        elif args.command == "search":
            _print(client.search(args.query, args.top_k, args.kb_id))
        elif args.command == "wiki" and args.wiki_action == "generate":
            _print(client.wiki_generate(args.kb_id))
        elif args.command == "queues":
            _print(client.queue_status())
        else:
            parser.print_help()
            return 2
        return 0
    except EkbClientError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
