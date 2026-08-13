"""
机密管理 — 用 Secrets Manager / 机密文件替代 .env 明文配置（P1）。

单一职责：在应用首次读取配置前，把外部机密源（文件系统 secrets / AWS Secrets
Manager）的内容注入 os.environ，使现有 pydantic-settings 配置加载无需改动即可
从机密源取值，避免敏感信息（DB 密码 / API Key / SECRET_KEY）明文落盘到 .env。

机密来源（按 SECRETS_PROVIDER 选择）：
    - env（默认）  ：仅从进程环境变量 / .env 读取（现状，无需外部依赖）；
                     若设置了 SECRETS_FILE_DIR，也会合并该目录下的机密文件；
    - file         ：从 SECRETS_FILE_DIR 目录读取机密文件
                     （Docker Swarm/K8s 挂载的 /run/secrets/*，文件名=变量名）；
    - aws          ：从 AWS Secrets Manager 读取（前缀 SECRETS_AWS_PREFIX 下的
                     每条 secret 的 key-value 注入环境变量）。

安全设计：
    - 幂等：apply_secrets 只执行一次，避免重复拉取；
    - 惰性：aws 提供方按需 import boto3，未配置时不引入依赖；
    - 失败降级：外部机密源不可用时记录告警并继续（保留 env 现值），
      绝不因机密源故障阻塞启动 —— 由运行时/运维告警兜底。
"""

from __future__ import annotations

import os
from typing import Any

_secrets_applied = False


def _log():
    """惰性获取 structlog logger。

    注意：不在模块顶层创建 logger —— get_settings() 首次调用时会触发本模块
    导入，而 get_logger() 内部会回调 get_settings()，顶层创建会形成循环导入
    （部分初始化的模块中 apply_secrets 尚不存在）。惰性获取可打破该环。
    """
    from app.utils.logger import get_logger

    return get_logger(__name__)


def _load_file_secrets(secret_dir: str) -> None:
    """从机密文件目录加载机密为环境变量（文件名 → 变量名，内容 → 值）。"""
    if not secret_dir or not os.path.isdir(secret_dir):
        return
    log = _log()
    for name in os.listdir(secret_dir):
        path = os.path.join(secret_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
            # 已由环境变量显式设置的值不覆盖（显式 env 优先）
            if name in os.environ:
                continue
            os.environ[name] = value
            log.info("secrets.file_loaded", name=name)
        except Exception as exc:
            log.warning("secrets.file_load_failed", name=name, error=str(exc))


def _load_aws_secrets(
    region: str, prefix: str, access_key: str | None, secret_key: str | None
) -> None:
    """从 AWS Secrets Manager 拉取机密并注入环境变量（惰性 import boto3）。"""
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:
        _log().warning("secrets.aws_boto3_missing", error=str(exc))
        return

    log = _log()
    try:
        kwargs: dict[str, Any] = {"region_name": region or None}
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        client = boto3.client("secretsmanager", **kwargs)
        prefix = prefix or ""

        paginator = client.get_paginator("list_secrets")
        for page in paginator.paginate():
            for secret in page.get("SecretList", []):
                name = secret.get("Name", "")
                if not prefix or name.startswith(prefix):
                    var_name = name[len(prefix):] if prefix else name
                    var_name = var_name.replace("/", "_").upper()
                    if not var_name:
                        continue
                    if var_name in os.environ:
                        continue  # 显式 env 优先
                    try:
                        value = client.get_secret_value(SecretId=name)["SecretString"]
                        os.environ[var_name] = value
                        log.info("secrets.aws_loaded", name=name, var=var_name)
                    except Exception as exc:
                        log.warning("secrets.aws_fetch_failed", name=name, error=str(exc))
    except Exception as exc:
        log.warning("secrets.aws_failed", error=str(exc)[:200])


def apply_secrets() -> None:
    """在首次读取配置前，将机密源内容注入环境变量（幂等）。"""
    global _secrets_applied
    if _secrets_applied:
        return
    _secrets_applied = True

    provider = os.environ.get("SECRETS_PROVIDER", "env").strip().lower()
    secret_dir = os.environ.get("SECRETS_FILE_DIR", "")

    # 兼容：即使 provider=env，若配置了机密文件目录也合并（Docker/K8s 挂载场景）
    if secret_dir:
        _load_file_secrets(secret_dir)

    if provider == "file":
        return  # 已由 secret_dir 处理

    if provider == "aws":
        _load_aws_secrets(
            region=os.environ.get("SECRETS_AWS_REGION", ""),
            prefix=os.environ.get("SECRETS_AWS_PREFIX", ""),
            access_key=os.environ.get("AWS_ACCESS_KEY_ID"),
            secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        return

    # provider == env：现状行为，无需额外处理
