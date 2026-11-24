"""Local configuration for an OpenAI- or Anthropic-compatible analysis endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


CONFIG_FILE = Path(__file__).resolve().parent / "data" / "analysis-config.json"
FORMATS = {"openai", "anthropic"}
REASONING_EFFORTS = {"auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"}


def endpoint_url(format_name: str, base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if format_name == "openai":
        if path.endswith("/chat/completions"):
            suffix = ""
        elif path.endswith("/v1"):
            suffix = "/chat/completions"
        elif path:
            suffix = "/chat/completions"
        else:
            suffix = "/v1/chat/completions"
    else:
        if path.endswith("/v1/messages"):
            suffix = ""
        elif path.endswith("/v1"):
            suffix = "/messages"
        elif path:
            suffix = "/v1/messages"
        else:
            suffix = "/v1/messages"
    return base_url.rstrip("/") + suffix


def models_url(format_name: str, base_url: str) -> str:
    endpoint = endpoint_url(format_name, base_url)
    suffix = "/chat/completions" if format_name == "openai" else "/messages"
    return endpoint.removesuffix(suffix) + "/models"


def validate_config(value: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, str]:
    format_name = str(value.get("format", "")).strip().lower()
    if format_name not in FORMATS:
        raise ValueError("分析接口格式请选择 OpenAI 兼容或 Anthropic 兼容。")
    base_url = str(value.get("base_url", "")).strip().rstrip("/")
    parsed = urlparse(base_url)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("分析接口链接须为 HTTPS；本机地址可用 HTTP。")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("分析接口链接须为服务基础地址或完整消息接口地址，不能含账号、查询参数或片段。")
    if (format_name == "anthropic" and parsed.path.rstrip("/").endswith("/chat/completions")) or \
       (format_name == "openai" and parsed.path.rstrip("/").endswith("/v1/messages")):
        raise ValueError("链接的消息接口路径与所选格式不匹配。")
    if len(base_url) > 500:
        raise ValueError("分析接口链接过长。")
    model = str(value.get("model", "")).strip()
    if not model or len(model) > 150:
        raise ValueError("请填写不超过 150 字符的模型名称。")
    reasoning_effort = str(value.get("reasoning_effort", "auto") or "auto").strip().lower()
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("思考强度不在所选接口的官方档位内。")
    if format_name == "anthropic" and reasoning_effort in {"none", "minimal"}:
        raise ValueError("Anthropic 接口不支持这个思考强度档位。")
    prior_key = ""
    if previous and previous.get("format") == format_name and previous.get("base_url") == base_url:
        prior_key = str(previous.get("api_key", "")).strip()
    api_key = str(value.get("api_key", "")).strip() or prior_key
    if not api_key or len(api_key) > 2048:
        raise ValueError("请填写分析接口 API Key。")
    return {"format": format_name, "base_url": base_url, "api_key": api_key, "model": model,
            "reasoning_effort": reasoning_effort}


def load_config() -> dict[str, str] | None:
    if not CONFIG_FILE.exists():
        return None
    value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("分析接口配置文件格式无效。")
    return validate_config(value)


def public_config() -> dict[str, Any]:
    config = load_config()
    if not config:
        return {"configured": False, "format": "openai", "base_url": "", "model": "",
                "reasoning_effort": "auto", "api_key_set": False}
    return {"configured": True, "format": config["format"], "base_url": config["base_url"],
            "model": config["model"], "reasoning_effort": config["reasoning_effort"],
            "api_key_set": bool(config["api_key"])}


def save_config(config: dict[str, str]) -> None:
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def clear_config() -> None:
    CONFIG_FILE.unlink(missing_ok=True)
