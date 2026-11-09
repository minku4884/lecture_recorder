import json
import shutil
import subprocess
import threading

import pytest

from recorder.common import Cancelled, safe_name
from recorder.config import Config
from recorder.instance import SingleInstance
from recorder.storage import Lesson, ProgressStore, course_key, remux_mp4


def lesson():
    return Lesson("unit4553", '강의 / 제목: 1?', "딥러닝이론", 0, 0, 0, True)


def test_resume_checks_file_size_and_does_not_use_site_done(tmp_path):
    unit = lesson()
    store = ProgressStore(tmp_path, "https://example.test/start", [unit])
    assert not store.completed(unit)
    path = store.directory / "test.mp4"
    path.write_bytes(b"recorded")
    store.mark(unit, "complete", path, 12)
    assert store.completed(unit)
    path.write_bytes(b"truncated")
    assert not store.completed(unit)
    path.unlink()
    assert not store.completed(unit)


def test_non_video_material_status_is_remembered(tmp_path):
    unit = lesson()
    store = ProgressStore(tmp_path, "https://example.test/start", [unit])
    store.mark(unit, "non_video", error="영상 없는 자료")
    assert store.non_video(unit)
    assert not store.completed(unit)


def test_archive_keeps_container_and_avoids_overwriting(tmp_path):
    unit, stop = lesson(), threading.Event()
    store = ProgressStore(tmp_path / "out", "https://example.test", [unit])
    source = tmp_path / "obs.mp4"
    source.write_bytes(b"video")
    first = store.archive(source, unit, True, stop)
    assert first.suffix == ".mp4" and not source.exists()
    source.write_bytes(b"second")
    second = store.archive(source, unit, True, stop)
    assert first.read_bytes() == b"video"
    assert "take02" in second.name
    source.write_bytes(b"partial")
    partial = store.archive(source, unit, False, stop)
    assert "INCOMPLETE" in partial.name
    store.mark(unit, "failed", partial)
    assert not store.completed(unit)


def test_course_identity_survives_lesson_url_change():
    unit = lesson()
    assert course_key("https://site.test/start?x=1", [unit]) == course_key("https://site.test/lesson/1", [unit])
    other = Lesson("unit4182", "different", "section", 0, 0, 0)
    assert course_key("https://site.test/start", [unit]) != course_key("https://site.test/start", [other])


@pytest.mark.parametrize("value", ["CON", "nul.txt", "COM1", "...", 'a/b:c*?"<d>|', "", "LPT9.txt"])
def test_windows_filenames(value):
    name = safe_name(value)
    assert name and name[-1] not in " ."
    assert not set(name) & set('<>:"/\\|?*')
    assert name.split(".")[0].upper() not in {"CON", "NUL", "COM1", "LPT9"}


def test_password_is_not_saved_in_plaintext(tmp_path):
    config = Config(obs_password="private-test-password", remember_password=False)
    path = tmp_path / "config.json"
    config.save(path)
    assert "private-test-password" not in path.read_text()
    assert "obs_password" not in json.loads(path.read_text())
    assert Config.load(path).obs_port == 4455


def test_single_instance_lock(tmp_path):
    from recorder.common import RecorderError
    with SingleInstance(tmp_path):
        with pytest.raises(RecorderError):
            with SingleInstance(tmp_path):
                pass
    with SingleInstance(tmp_path):
        pass


def test_real_remux_preserves_media_and_original(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg required for optional remux tests")
    fixture = __import__("pathlib").Path(__file__).parent / "fixtures" / "two.mp4"
    source = tmp_path / "original.mkv"
    subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-i", str(fixture), "-c", "copy", str(source)], check=True)
    target = remux_mp4(source, ffmpeg, threading.Event())
    assert source.exists() and target.suffix == ".mp4" and target.stat().st_size > 0
    metadata = json.loads(subprocess.check_output([ffprobe, "-v", "quiet", "-show_format", "-show_streams", "-of", "json", str(target)]))
    assert abs(float(metadata["format"]["duration"]) - 2.8) < 0.15
    assert {stream["codec_type"] for stream in metadata["streams"]} == {"video", "audio"}
