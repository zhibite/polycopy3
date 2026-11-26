"""Serialise AppConfig back to TOML (stdlib only) with an atomic write."""
from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

from ..config import (AppConfig, LiveConfig, PaperConfig, RunConfig,
                      TargetConfig)

TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot serialise {type(value).__name__} to TOML")


def dumps_config(cfg: AppConfig) -> str:
    lines: list[str] = [
        "# polycopy copy-trading configuration",
        "# Edited via the dashboard; the engine hot-reloads strategy fields.",
        "# run.mode / live.* changes require a bot restart.",
        "",
        "[account]",
        f"starting_cash = {_toml_value(cfg.account.starting_cash)}",
        "",
        "[paper]",
    ]
    for field in fields(PaperConfig):
        value = getattr(cfg.paper, field.name)
        if value is not None:
            lines.append(f"{field.name} = {_toml_value(value)}")
    lines.extend(["", "[run]"])
    for field in fields(RunConfig):
        lines.append(
            f"{field.name} = {_toml_value(getattr(cfg.run, field.name))}")
    lines.extend(["", "[live]"])
    for field in fields(LiveConfig):
        value = getattr(cfg.live, field.name)
        if value is not None:
            lines.append(f"{field.name} = {_toml_value(value)}")
    for name, target in cfg.targets.items():
        lines.extend(["", f"[targets.{name}]"])
        for field in fields(TargetConfig):
            value = getattr(target, field.name)
            if value is not None:
                lines.append(f"{field.name} = {_toml_value(value)}")
    lines.append("")
    return "\n".join(lines)


def save_config(cfg: AppConfig, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for name in cfg.targets:
        if not TARGET_NAME_RE.match(str(name)):
            raise ValueError(
                f"target name {name!r} must match [A-Za-z0-9_-]{{1,32}}")
    text = dumps_config(cfg)
    # Validate before touching the live file.
    parsed = tomllib.loads(text)
    if not parsed.get("targets"):
        raise ValueError("generated config has no targets")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)
