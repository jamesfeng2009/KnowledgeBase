#!/usr/bin/env python
"""AutoDL 容器实例 Pro 管理 — 创建/查询/开关机/释放。

把 13 项训练修复后的模型放到 AutoDL GPU 上重训（本地 M3 Max MPS 太慢）。
本脚本封装 api.md 中的 /api/v1/dev/instance/pro/* 接口，仅依赖标准库，
可在任何 Python3.8+ 环境运行（无需 backend venv）。

Token 读取顺序（优先级递减）：
    1. --token 参数
    2. AUTODL_API_TOKEN 环境变量
    3. frontend/.env 中的 AUTODL_API_TOKEN 行

用法：
    # 创建 4090D 实例（1.5B 训练，默认 PyTorch cuda11.8 镜像 + 50G 扩容）
    python scripts/autodl/manage.py create

    # 等待实例 running 并打印 SSH 连接信息
    python scripts/autodl/manage.py wait pro-76576c61fdf1
    python scripts/autodl/manage.py detail pro-76576c61fdf1

    # 训练结束后关机省钱 / 彻底释放
    python scripts/autodl/manage.py power_off pro-76576c61fdf1
    python scripts/autodl/manage.py release pro-76576c61fdf1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_HOST = "https://api.autodl.com"
TIMEOUT = 30

#: 默认 GPU 规格（1.5B LoRA/DPO/GRPO 全流程够用，24G 显存）
DEFAULT_GPU_SPEC = "4090D"
#: 默认镜像：PyTorch cuda11.8-cudnn8-devel-ubuntu20.04-py38-torch2.0.0
#: 创建后 pip 升级到 torch>=2.2（trl>=0.16 要求）；cuda11.8 与 torch2.2+cuda118 wheel 对齐
DEFAULT_IMAGE = "base-image-l2t43iu6uk"
DEFAULT_DISK_GB = 50
DEFAULT_CUDA_V = 118


# ------------------------------------------------------------------
# Token 与 HTTP
# ------------------------------------------------------------------

def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_token(cli_token: str | None) -> str:
    """按优先级解析 AutoDL 开发者 token。"""
    if cli_token:
        return cli_token.strip()
    env = os.environ.get("AUTODL_API_TOKEN")
    if env:
        return env.strip()
    env_file = _project_root() / "frontend" / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("AUTODL_API_TOKEN="):
                val = line.split("=", 1)[1].strip()
                if val:
                    return val
    sys.exit("错误：未找到 token。请用 --token 传入，或设置 AUTODL_API_TOKEN 环境变量，"
             "或在 frontend/.env 中配置 AUTODL_API_TOKEN")


def _request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """调用 AutoDL API。

    AutoDL 接口约定：GET 请求的参数走 query string（不读 body），POST 请求
    要求 Content-Type: application/json 且必须有 body（空接口发 {}）。
    """
    url = f"{API_HOST}{path}"
    if method == "GET":
        params = body or {}
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = None
    else:
        data = json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8"))
        except Exception:
            err = {"raw": str(exc)}
        sys.exit(f"HTTP {exc.code} 调用失败 {path}: {err}")
    except urllib.error.URLError as exc:
        sys.exit(f"网络错误 {path}: {exc.reason}")
    if payload.get("code") != "Success":
        sys.exit(f"API 返回失败 {path}: code={payload.get('code')} msg={payload.get('msg')}")
    return payload


# ------------------------------------------------------------------
# 子命令
# ------------------------------------------------------------------

def cmd_balance(args: argparse.Namespace, token: str) -> None:
    """查询账户余额（充值后核对到账）。"""
    r = _request("POST", "/api/v1/dev/wallet/balance", token)
    d = r["data"]
    assets_yuan = d["assets"] / 1000
    voucher_yuan = d["voucher_balance"] / 1000
    print(f"账户余额：{assets_yuan:.2f} 元 | 代金券：{voucher_yuan:.2f} 元 | "
          f"累计消费：{d['accumulate'] / 1000:.2f} 元")
    if assets_yuan < 1 and voucher_yuan < 1:
        print("⚠️  余额不足 1 元，无法创建实例，请先充值。")


def cmd_create(args: argparse.Namespace, token: str) -> None:
    """创建实例（默认按量计费）。"""
    body = {
        "req_gpu_amount": args.gpu_amount,
        "expand_system_disk_by_gb": args.disk_gb,
        "gpu_spec_uuid": args.gpu_spec,
        "image_uuid": args.image,
        "cuda_v_from": args.cuda_v,
        "instance_name": args.name,
    }
    if args.region:
        body["data_center_list"] = args.region
    print(f"创建实例：GPU={args.gpu_spec} ×{args.gpu_amount}  镜像={args.image}  "
          f"系统盘+{args.disk_gb}GB  cuda>={args.cuda_v}")
    r = _request("POST", "/api/v1/dev/instance/pro/create", token, body)
    instance_uuid = r["data"]
    print(f"✅ 创建成功：{instance_uuid}")
    print(f"   下一步：python {Path(__file__).name} wait {instance_uuid}")


def cmd_status(args: argparse.Namespace, token: str) -> None:
    r = _request("GET", "/api/v1/dev/instance/pro/status", token,
                 {"instance_uuid": args.uuid})
    print(f"{args.uuid}: {r['data']}")


def cmd_detail(args: argparse.Namespace, token: str) -> None:
    """获取实例详情：SSH 连接信息 / jupyter / 资源占用。"""
    r = _request("GET", "/api/v1/dev/instance/pro/snapshot", token,
                 {"instance_uuid": args.uuid})
    d = r["data"]
    u = d.get("usage_info", {})
    print(f"=== 实例 {args.uuid} ===")
    print(f"GPU:    {d.get('snapshot_gpu_alias_name', '?')}")
    print(f"区域:   {d.get('region_sign', '?')}")
    print(f"按量价: {d.get('payg_price', 0) / 1000:.2f} 元/小时")
    print(f"状态:   {u.get('valid', False)}")
    print(f"SSH:    {d.get('ssh_command', '?')}")
    print(f"密码:   {d.get('root_password', '?')}")
    print(f"Jupyter: {d.get('jupyter_domain', '?')}")
    print(f"  token: {d.get('jupyter_token', '?')}")
    print(f"内存:   {u.get('mem_usage_percent', 0):.1f}% / "
          f"{u.get('mem_limit', 0) / 1024 / 1024 / 1024:.1f} GB")
    print(f"磁盘:   根分区 {u.get('root_fs_used_size', 0) / 1024 / 1024 / 1024:.1f} / "
          f"{u.get('root_fs_total_size', 0) / 1024 / 1024 / 1024:.1f} GB")


def cmd_wait(args: argparse.Namespace, token: str) -> None:
    """轮询实例状态直到 running（创建后镜像拉取需 1-3 分钟）。"""
    deadline = time.time() + args.timeout
    last = None
    while time.time() < deadline:
        r = _request("GET", "/api/v1/dev/instance/pro/status", token,
                     {"instance_uuid": args.uuid})
        status = r["data"]
        if status != last:
            print(f"[{time.strftime('%H:%M:%S')}] 状态: {status}")
            last = status
        if status == "running":
            print(f"✅ 实例已运行，获取连接信息...")
            # 复用 detail 打印 SSH 信息
            args_detail = argparse.Namespace(uuid=args.uuid)
            cmd_detail(args_detail, token)
            return
        time.sleep(args.interval)
    sys.exit(f"⏰ 等待超时（{args.timeout}s），实例仍未 running。"
             f"当前状态: {last}（可能仍在拉取镜像，稍后重试 wait）")


def cmd_power_on(args: argparse.Namespace, token: str) -> None:
    _request("POST", "/api/v1/dev/instance/pro/power_on", token,
             {"instance_uuid": args.uuid, "payload": "gpu"})
    print(f"✅ 已发送开机指令：{args.uuid}")


def cmd_power_off(args: argparse.Namespace, token: str) -> None:
    _request("POST", "/api/v1/dev/instance/pro/power_off", token,
             {"instance_uuid": args.uuid})
    print(f"✅ 已发送关机指令：{args.uuid}（关机后不计 GPU 费用，仅收磁盘存储费）")


def cmd_release(args: argparse.Namespace, token: str) -> None:
    if not args.yes:
        confirm = input(f"确认释放实例 {args.uuid}？此操作不可逆，数据将全部丢失 [y/N]: ")
        if confirm.lower() != "y":
            print("已取消")
            return
    _request("POST", "/api/v1/dev/instance/pro/release", token,
             {"instance_uuid": args.uuid})
    print(f"✅ 已释放实例：{args.uuid}")


def cmd_list(args: argparse.Namespace, token: str) -> None:
    r = _request("POST", "/api/v1/dev/instance/pro/list", token,
                 {"page_index": 1, "page_size": 50})
    items = r["data"]["list"]
    if not items:
        print("无实例")
        return
    print(f"{'UUID':<22} {'状态':<10} {'GPU规格':<12} {'名称':<16} {'创建时间'}")
    for it in items:
        print(f"{it['uuid']:<22} {it['status']:<10} {it.get('gpu_spec_uuid', '?'):<12} "
              f"{it.get('name', ''):<16} {it['created_at']}")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AutoDL 容器实例 Pro 管理（创建/查询/开关机/释放）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--token", default=None, help="AutoDL 开发者 token（默认从 .env/环境变量读）")
    sub = p.add_subparsers(dest="cmd", required=True)

    # balance
    sub.add_parser("balance", help="查询账户余额").set_defaults(func=cmd_balance)

    # create
    pc = sub.add_parser("create", help="创建实例")
    pc.add_argument("--gpu_spec", default=DEFAULT_GPU_SPEC,
                   help="GPU 规格ID（4090D / v-48g-350w / pro6000-p / h800 等）")
    pc.add_argument("--gpu_amount", type=int, default=1, help="GPU 数量")
    pc.add_argument("--image", default=DEFAULT_IMAGE, help="镜像 UUID")
    pc.add_argument("--disk_gb", type=int, default=DEFAULT_DISK_GB, help="系统盘扩容 GB")
    pc.add_argument("--cuda_v", type=int, default=DEFAULT_CUDA_V, help="CUDA 版本下限")
    pc.add_argument("--name", default="ekb-finetune", help="实例备注名")
    pc.add_argument("--region", nargs="+", default=None,
                   help="地区（westDC3 西北 / beijingDC2 北京），默认自动选")
    pc.set_defaults(func=cmd_create)

    # status
    ps = sub.add_parser("status", help="查询实例状态")
    ps.add_argument("uuid", help="实例 UUID")
    ps.set_defaults(func=cmd_status)

    # detail
    pd = sub.add_parser("detail", help="获取实例详情（SSH/Jupyter/资源）")
    pd.add_argument("uuid", help="实例 UUID")
    pd.set_defaults(func=cmd_detail)

    # wait
    pw = sub.add_parser("wait", help="等待实例 running")
    pw.add_argument("uuid", help="实例 UUID")
    pw.add_argument("--interval", type=int, default=10, help="轮询间隔秒")
    pw.add_argument("--timeout", type=int, default=600, help="最长等待秒数")
    pw.set_defaults(func=cmd_wait)

    # power_on / power_off / release
    pon = sub.add_parser("power_on", help="开机（有卡模式）")
    pon.add_argument("uuid", help="实例 UUID")
    pon.set_defaults(func=cmd_power_on)

    poff = sub.add_parser("power_off", help="关机（省钱）")
    poff.add_argument("uuid", help="实例 UUID")
    poff.set_defaults(func=cmd_power_off)

    prel = sub.add_parser("release", help="释放实例（不可逆）")
    prel.add_argument("uuid", help="实例 UUID")
    prel.add_argument("--yes", action="store_true", help="跳过确认")
    prel.set_defaults(func=cmd_release)

    # list
    sub.add_parser("list", help="列出全部实例").set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = load_token(args.token)
    args.func(args, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
