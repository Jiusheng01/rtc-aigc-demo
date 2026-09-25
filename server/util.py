# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def read_files(directory: str | Path, suffix: str) -> dict[str, Any]:
    base = Path(directory)
    scenes: dict[str, Any] = {}
    for path in sorted(base.iterdir()):
        if path.name.endswith(suffix):
            scenes[path.name[: -len(suffix)]] = json.loads(path.read_text(encoding="utf-8"))
    return scenes


def assert_value(expression: Any, message: str) -> None:
    invalid = not expression
    if isinstance(expression, str) and " " in expression:
        invalid = True
    if invalid:
        print(f"\033[31m校验失败: {message}\033[0m")
        raise ValueError(message)


async def wrapper(
    *,
    method: str,
    path: str,
    api_name: str,
    logic: Callable[[], Any],
    contain_response_metadata: bool = True,
) -> tuple[bool, dict[str, Any]]:
    if method.lower() != "post" or not path.startswith(f"/{api_name}"):
        return False, {}

    response_metadata: dict[str, Any] = {"Action": api_name}
    try:
        result = logic()
        if hasattr(result, "__await__"):
            result = await result
        if contain_response_metadata:
            return True, {"ResponseMetadata": response_metadata, "Result": result}
        return True, result
    except Exception as error:
        response_metadata["Error"] = {
            "Code": -1,
            "Message": f"Error: {error}",
        }
        return True, {"ResponseMetadata": response_metadata}
