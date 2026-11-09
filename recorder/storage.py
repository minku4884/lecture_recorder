from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .common import Cancelled, RecorderError, atomic_json, safe_name, sleep_checked, unique_path


@dataclass(frozen=True)
class Lesson:
    unit_id: str
    title: str
    section_title: str
    section_index: int
    unit_index: int
    global_index: int
    site_done: bool = False


def course_key(url: str, lessons: list[Lesson]) -> str:
    parsed = urlsplit(url)
    # /start/ can be shared by several courses. The first site's unit ID distinguishes them.
    identity = f"{parsed.netloc.lower()}|{lessons[0].unit_id}"
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


def check_disk(path: Path, minimum_mb: int) -> None:
    parent = path
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if shutil.disk_usage(parent).free < minimum_mb * 1024 * 1024:
        raise RecorderError(f"디스크 여유 공간이 {minimum_mb} MB보다 작습니다: {parent}")


class ProgressStore:
    def __init__(self, output: Path, url: str, lessons: list[Lesson]):
        if not lessons:
            raise RecorderError("강의 목록이 비어 있습니다.")
        self.directory = output / ("course_" + course_key(url, lessons))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "progress.json"
        self.data = {"schema": 1, "lessons": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if self.data.get("schema") != 1 or not isinstance(self.data.get("lessons"), dict):
                    raise ValueError("unknown schema")
            except Exception as error:
                raise RecorderError("progress.json을 읽을 수 없습니다. 새 저장 폴더를 선택하세요.") from error
        atomic_json(self.directory / "course_catalog.json", [asdict(lesson) for lesson in lessons])

    def completed(self, lesson: Lesson) -> bool:
        item = self.data["lessons"].get(lesson.unit_id, {})
        if item.get("status") != "complete":
            return False
        path = self.directory / item.get("file", "")
        try:
            return path.is_file() and path.stat().st_size == item.get("size") and item["size"] > 0
        except (OSError, KeyError):
            return False

    def non_video(self, lesson: Lesson) -> bool:
        return self.data["lessons"].get(lesson.unit_id, {}).get("status") == "non_video"

    def mark(self, lesson: Lesson, status: str, path: Path | None = None,
             duration: float = 0, error: str = "") -> None:
        entry = {"title": lesson.title, "section": lesson.section_title, "status": status,
                 "updated_at": datetime.now(timezone.utc).isoformat(), "duration": duration}
        if path:
            entry.update(file=str(path.relative_to(self.directory)), size=path.stat().st_size)
        if error:
            entry["error"] = error
        self.data["lessons"][lesson.unit_id] = entry
        atomic_json(self.path, self.data)

    def archive(self, source: Path, lesson: Lesson, complete: bool,
                stop: threading.Event, timeout: float = 30) -> Path:
        if source.suffix.lower() not in (".mkv", ".mp4", ".mov", ".flv", ".ts", ".avi"):
            raise RecorderError(f"OBS 녹화 확장자를 확인하세요: {source.suffix}")
        # StopRecord's exact outputPath is authoritative; never guess the newest file.
        deadline, last_size, stable = time.monotonic() + timeout, -1, 0
        while time.monotonic() < deadline:
            try:
                size = source.stat().st_size
                stable = stable + 1 if size == last_size and size > 0 else 0
                last_size = size
                if stable >= 2:
                    break
            except OSError:
                pass
            # Preserve an incomplete file even when the user has pressed Stop.
            time.sleep(0.2)
        else:
            raise RecorderError("OBS 녹화 파일을 읽을 수 없습니다. OBS 원래 녹화 폴더를 확인하세요.")
        folder = self.directory / f"{lesson.section_index + 1:02d}_{safe_name(lesson.section_title, 45)}"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = "" if complete else "__INCOMPLETE"
        title_limit = max(20, min(75, 230 - len(str(folder)) - len(lesson.unit_id) - 30))
        target = unique_path(folder / f"{lesson.unit_index + 1:03d}_{safe_name(lesson.title, title_limit)}__{safe_name(lesson.unit_id, 24)}{suffix}{source.suffix.lower()}")
        if source.resolve() == target.resolve():
            return target
        # Copy to a temporary destination before removing the source, including across drives.
        temporary = target.with_name(target.name + ".copying")
        try:
            shutil.copy2(source, temporary)
            if temporary.stat().st_size != size:
                raise RecorderError("녹화 파일 복사 크기가 일치하지 않습니다.")
            temporary.replace(target)
            source.unlink()
        except Exception as error:
            if temporary.exists():
                temporary.unlink()
            raise RecorderError(f"녹화 파일 이동에 실패했습니다. 원본을 확인하세요: {source}") from error
        return target


def remux_mp4(source: Path, ffmpeg: str, stop: threading.Event) -> Path:
    if source.suffix.lower() == ".mp4":
        return source
    target = unique_path(source.with_suffix(".mp4"))
    temporary = target.with_name(target.stem + ".remuxing.mp4")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
               "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart", str(temporary)]
    # Write stderr to a file to avoid filling a PIPE while polling a long conversion.
    diagnostic = source.with_name(source.name + ".ffmpeg.log")
    try:
        with diagnostic.open("w", encoding="utf-8") as errors:
            try:
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors,
                                           creationflags=0x08000000 if __import__("os").name == "nt" else 0)
            except OSError as error:
                raise RecorderError("FFmpeg를 실행하지 못했습니다. 경로를 확인하세요. 원본 녹화는 보존했습니다.") from error
            deadline = time.monotonic() + 1800
            try:
                while process.poll() is None:
                    if time.monotonic() > deadline:
                        raise RecorderError("MP4 변환 시간이 초과됐습니다. 원본 파일은 보존됩니다.")
                    sleep_checked(0.25, stop)
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise
        if process.returncode != 0 or not temporary.exists() or temporary.stat().st_size == 0:
            raise RecorderError("MP4 변환에 실패했습니다. 원본 파일과 ffmpeg 로그를 보존했습니다.")
        temporary.replace(target)
        diagnostic.unlink(missing_ok=True)
        # Keep the original recording alongside MP4; users can remove it after checking the result.
        return target
    finally:
        temporary.unlink(missing_ok=True)
