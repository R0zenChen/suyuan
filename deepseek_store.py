"""Local DeepSeek Search / Fetch credentials, with legacy .env fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CONFIG_FILE = Path(__file__).resolve().parent / "data" / "deepseek-config.json"
REASONING_EFFORTS = {"auto", "low", "high", "max"}


def load_config() -> dict[str, str] | None:
    if not CONFIG_FILE.exists():
        return None
    value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("DeepSeek 配置文件格式无效。")
    return validate_config(value)


def validate_config(value: dict[str, Any], previous: dict[str, str] | None = None) -> dict[str, str]:
    model = str(value.get("model", "")).strip()
    if not model or len(model) > 150:
        raise ValueError("请填写 DeepSeek 模型名称。")
    reasoning_effort = str(value.get("reasoning_effort", "auto") or "auto").strip().lower()
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError("DeepSeek 思考强度请选择自动、低、高或最大。")
    api_key = str(value.get("api_key", "")).strip() or (previous or {}).get("api_key", "")
    if not api_key or len(api_key) > 2048:
        raise ValueError("请填写 DeepSeek API Key。")
    return {"api_key": api_key, "model": model, "reasoning_effort": reasoning_effort}


def save_config(config: dict[str, str]) -> None:
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def clear_config() -> None:
    CONFIG_FILE.unlink(missing_ok=True)
