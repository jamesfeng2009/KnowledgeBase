"""SSE 首 token（TTFT）压测 — 让「SSE 首 token <500ms」可复现、可量化。

单一职责：对 POST /api/v1/chat/stream 发起并发 SSE 请求，
统计 Time-To-First-Token 分布（min/p50/p90/p95/p99/max + <500ms 达标率）。

用法：
    # 方式 1：直接传 JWT
    python scripts/bench_ttft.py --token "$EKB_JWT_TOKEN"

    # 方式 2：账号密码自动登录
    python scripts/bench_ttft.py --login admin@example.com -p admin123

    # 自定义并发与请求量（默认 20 并发 × 2 轮 = 40 请求）
    python scripts/bench_ttft.py --login ... --concurrency 20 --requests 100

压测逻辑说明：
    - TTFT = 从 HTTP 请求发出到收到首个「内容 chunk」（data: 行，
      排除 heartbeat 注释与空行）的毫秒数 — 与浏览器 EventSource
      感知一致（服务端 sse.ttft 埋点同口径）。
    - 并发模型：固定 worker 池，每个 worker 串行执行分到的请求
      （真实模拟持续负载，而非瞬时脉冲）。
    - 结果输出 evals/results/ttft_benchmark.json（可用 --output 覆盖），
      与 eval_metric* 系列一样作为可追溯的评测存档。

依赖：httpx（backend 已有）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx

# 压测查询池 — 覆盖短/中/长查询，避免单一 query 命中缓存造成 TTFT 虚低
DEFAULT_QUERIES: list[str] = [
    "公司的报销流程是什么？",
    "如何申请服务器资源？帮我梳理一下完整步骤。",
    "总结一下上个季度的产品规划要点，并列出关键的里程碑时间。",
    "新员工入职需要准备哪些材料？",
    "数据安全规范里对敏感数据的分级要求是什么？",
]

# 目标达标线（简历口径：SSE 首 token < 500ms）
TTFT_TARGET_MS: float = 500.0


def _percentile(sorted_vals: list[float], p: float) -> float:
    """线性插值百分位（p ∈ [0, 100]）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


async def _login(base_url: str, email: str, password: str) -> str:
    """登录获取 JWT access_token。"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or {}
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"登录响应缺少 access_token: {payload}")
        return token


async def _measure_one(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    query: str,
) -> dict:
    """执行一次 SSE 请求，返回 {ttft_ms, total_ms, ok, error}。

    TTFT 口径：请求发出 → 首个内容 data 行到达（排除 heartbeat / 注释行）。
    """
    t0 = time.monotonic()
    ttft_ms: float | None = None
    error: str | None = None
    try:
        async with client.stream(
            "POST",
            f"{base_url}/api/v1/chat/stream",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
            },
            json={"query": query},
        ) as resp:
            if resp.status_code != 200:
                error = f"HTTP {resp.status_code}"
            else:
                async for line in _aiter_lines(resp):
                    line = line.strip()
                    # 心跳/注释/空行不算首 token
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        ttft_ms = (time.monotonic() - t0) * 1000
                        break
                if ttft_ms is None and error is None:
                    error = "stream ended without data"
    except Exception as exc:  # noqa: BLE001 — 压测需要吞掉单次失败继续
        error = str(exc)[:200]

    total_ms = (time.monotonic() - t0) * 1000
    return {
        "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
        "total_ms": round(total_ms, 1),
        "ok": error is None,
        "error": error,
    }


async def _aiter_lines(resp: httpx.Response) -> AsyncIterator[str]:
    """按行读取 SSE 流（兼容 \\r\\n）。"""
    buffer = ""
    async for chunk in resp.aiter_text():
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line.rstrip("\r")
    if buffer:
        yield buffer


async def _worker(
    worker_id: int,
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    queries: list[str],
    request_indices: list[int],
    results: list[dict],
) -> None:
    """单个 worker：串行执行分配到的请求编号。"""
    for idx in request_indices:
        query = queries[idx % len(queries)]
        result = await _measure_one(client, base_url, token, query)
        result["request_id"] = idx
        result["worker_id"] = worker_id
        results.append(result)


def _summarize(results: list[dict], concurrency: int) -> dict:
    """汇总 TTFT 统计。"""
    ok_results = [r for r in results if r["ok"] and r["ttft_ms"] is not None]
    ttfts = sorted(r["ttft_ms"] for r in ok_results)  # type: ignore[misc]
    met = sum(1 for t in ttfts if t < TTFT_TARGET_MS)

    summary: dict = {
        "target_ms": TTFT_TARGET_MS,
        "concurrency": concurrency,
        "total_requests": len(results),
        "ok_requests": len(ok_results),
        "failed_requests": len(results) - len(ok_results),
    }
    if ttfts:
        summary.update(
            {
                "ttft_min_ms": round(ttfts[0], 1),
                "ttft_p50_ms": round(_percentile(ttfts, 50), 1),
                "ttft_p90_ms": round(_percentile(ttfts, 90), 1),
                "ttft_p95_ms": round(_percentile(ttfts, 95), 1),
                "ttft_p99_ms": round(_percentile(ttfts, 99), 1),
                "ttft_max_ms": round(ttfts[-1], 1),
                "ttft_mean_ms": round(statistics.fmean(ttfts), 1),
                "ttft_met_target": met,
                "ttft_met_rate": round(met / len(ttfts), 4),
            }
        )
    if len(results) > len(ok_results):
        summary["errors"] = [
            r["error"] for r in results if not r["ok"]
        ][:10]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="SSE TTFT 并发压测")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", default="", help="JWT（优先于 --login）")
    parser.add_argument("--login", default="", help="登录邮箱（配合 -p）")
    parser.add_argument("-p", "--password", default="")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests", type=int, default=40,
                        help="总请求数（默认 40 = 20 并发 × 2 轮）")
    parser.add_argument("--output",
                        default="evals/results/ttft_benchmark.json",
                        help="结果 JSON 输出路径")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")

    async def run() -> tuple[list[dict], str]:
        token = args.token
        if not token:
            if not args.login or not args.password:
                parser.error("需要 --token 或 --login + -p 提供鉴权")
            token = await _login(base_url, args.login, args.password)
            print(f"✓ 登录成功（{args.login}）")

        print(f"\n{'=' * 70}")
        print(f"SSE TTFT 压测 — {base_url}")
        print(f"    并发 {args.concurrency} × 请求 {args.requests}，"
              f"目标 TTFT < {TTFT_TARGET_MS:.0f}ms")
        print(f"{'=' * 70}\n", flush=True)

        results: list[dict] = []
        limits = httpx.Limits(max_connections=args.concurrency + 10,
                              max_keepalive_connections=args.concurrency)
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=30.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            # 请求编号轮转分配给 worker（i % concurrency）
            buckets: list[list[int]] = [[] for _ in range(args.concurrency)]
            for i in range(args.requests):
                buckets[i % args.concurrency].append(i)
            t0 = time.monotonic()
            await asyncio.gather(*[
                _worker(w, client, base_url, token, DEFAULT_QUERIES,
                        bucket, results)
                for w, bucket in enumerate(buckets)
            ])
            wall_ms = (time.monotonic() - t0) * 1000
        return results, f"{wall_ms:.0f}"

    results, wall_ms = asyncio.run(run())
    summary = _summarize(results, args.concurrency)
    summary["wall_time_ms"] = float(wall_ms)
    summary["throughput_rps"] = round(
        args.requests / (float(wall_ms) / 1000.0), 2
    ) if float(wall_ms) > 0 else 0.0

    print("--- 结果 ---")
    for key, value in summary.items():
        print(f"  {key:24s}: {value}")
    print(f"{'=' * 70}\n")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "summary": summary,
        "results": results,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"结果已保存到 {out_path}")

    # 退出码：有失败请求或未达标的请求占比 > 10% 时返回 1（CI 可用）
    if summary.get("failed_requests", 0) > 0:
        return 1
    if ttfts := [r["ttft_ms"] for r in results
                 if r["ok"] and r["ttft_ms"] is not None]:
        met_rate = sum(1 for t in ttfts if t < TTFT_TARGET_MS) / len(ttfts)
        return 0 if met_rate >= 0.9 else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
