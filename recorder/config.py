from __future__ import annotations

import base64
import ctypes
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

from .common import RecorderError, atomic_json


def _dpapi(value: bytes, decrypt: bool = False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    incoming = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypto.CryptUnprotectData
        args = (ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        fn = crypto.CryptProtectData
        args = (ctypes.byref(incoming), "Lecture Recorder", None, None, None, 1, ctypes.byref(outgoing))
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
                   ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(*args):
        raise RecorderError("OBS 비밀번호 저장/읽기에 실패했습니다. 비밀번호를 다시 입력하세요.")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel.LocalFree(outgoing.pbData)


@dataclass
class Config:
    course_url: str = ""
    browser: str = "chrome"
    output_dir: str = "recordings"
    obs_host: str = "127.0.0.1"
    obs_port: int = 4455
    obs_password: str = ""
    remember_password: bool = True
    auto_scene: bool = False
    resume: bool = True
    remux_mp4: bool = True
    playback_only: bool = False
    continue_on_error: bool = False
    minimize_on_record: bool = True
    fullscreen_video: bool = False
    volume: float = 1.0
    load_timeout: float = 40.0
    material_timeout: float = 8.0
    stall_timeout: float = 60.0
    max_lesson_seconds: float = 21600.0
    retries: int = 1
    min_free_mb: int = 1024
    ffmpeg_path: str = ""
    timeline_selector: str = ".course_timeline.accordion > ul"
    video_selector: str = "video#main-video"

    @classmethod
    def load(cls, path: Path) -> "Config":
        import json

        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            known = {field.name for field in fields(cls)}
            cfg = cls(**{key: value for key, value in data.items() if key in known})
            encrypted = data.get("obs_password_dpapi", "")
            if encrypted and os.name == "nt":
                try:
                    cfg.obs_password = _dpapi(base64.b64decode(encrypted), decrypt=True).decode("utf-8")
                except Exception:
                    cfg.obs_password = ""
            cfg.obs_password = os.environ.get("OBS_PASSWORD", cfg.obs_password)
            return cfg
        except Exception as error:
            raise RecorderError(f"설정 파일을 읽을 수 없습니다: {path.name}") from error

    def save(self, path: Path) -> None:
        data = asdict(self)
        data.pop("obs_password")
        if self.remember_password and self.obs_password and os.name == "nt":
            data["obs_password_dpapi"] = base64.b64encode(_dpapi(self.obs_password.encode())).decode()
        atomic_json(path, data)

    def output_path(self, base: Path) -> Path:
        path = Path(self.output_dir).expanduser()
        return (path if path.is_absolute() else base / path).resolve()

    def validate(self, need_url: bool = True) -> None:
        if need_url:
            parsed = urlsplit(self.course_url.strip())
            if parsed.scheme not in ("https", "http") or not parsed.hostname:
                raise RecorderError("브라우저 주소창에서 복사한 강의 주소(http/https)를 입력하세요.")
        if self.browser not in ("chrome", "msedge", "chromium"):
            raise RecorderError("브라우저는 Chrome, Edge, Chromium 중에서 선택하세요.")
        if self.obs_host not in ("localhost", "127.0.0.1", "::1"):
            raise RecorderError("OBS는 이 PC에서 실행하세요. 호스트는 127.0.0.1로 설정하세요.")
        if not 1 <= int(self.obs_port) <= 65535:
            raise RecorderError("OBS 포트는 1~65535 사이여야 합니다.")
        if not 0 < self.volume <= 1:
            raise RecorderError("volume은 0보다 크고 1 이하여야 합니다.")
        if self.load_timeout <= 0 or self.material_timeout <= 0 or self.stall_timeout <= 0 or self.max_lesson_seconds <= 0:
            raise RecorderError("대기 시간 설정은 0보다 커야 합니다.")
        if not 0 <= self.retries <= 5 or self.min_free_mb < 0:
            raise RecorderError("retries는 0~5, min_free_mb는 0 이상이어야 합니다.")
