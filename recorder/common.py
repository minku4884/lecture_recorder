from __future__ import annotations

import ctypes
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path


class RecorderError(RuntimeError):
    pass


class LectureError(RecorderError):
    """A lesson can be retried from its beginning."""


class NonVideoLesson(RecorderError):
    """The selected course item is reading material rather than a video."""


class Cancelled(RecorderError):
    pass


def check_stop(stop: threading.Event) -> None:
    if stop.is_set():
        raise Cancelled("사용자가 중지했습니다.")


def sleep_checked(seconds: float, stop: threading.Event) -> None:
    if stop.wait(max(0, seconds)):
        raise Cancelled("사용자가 중지했습니다.")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_name(value: str, limit: int = 80) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")[:limit].rstrip(" .")
    if not value:
        value = "untitled"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    return value


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for take in range(2, 10000):
        candidate = path.with_name(f"{path.stem}__take{take:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RecorderError("같은 파일명이 너무 많습니다. 다른 저장 폴더를 선택하세요.")


def finite_number(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def clock(seconds: float) -> str:
    seconds = max(0, int(finite_number(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class KeepAwake:
    """Keep the display and system awake only while this Windows thread is recording."""

    def __enter__(self):
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
        return self

    def __exit__(self, *_):
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
