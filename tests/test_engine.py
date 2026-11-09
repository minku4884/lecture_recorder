import json
from dataclasses import replace

from recorder.common import NonVideoLesson
from recorder.config import Config
from recorder.engine import RecordingEngine
from recorder.storage import Lesson, ProgressStore


def test_end_to_end_recording_resume_and_missing_file(cfg, tmp_path, fake_obs, browser_factory):
    cfg = replace(cfg, obs_port=fake_obs.port, obs_password=fake_obs.password)
    events = []
    engine = RecordingEngine(tmp_path / "app", events.append, browser_factory=browser_factory)
    try:
        engine.open_browser(cfg)
        engine.read_catalog(cfg)
        selected = [lesson.unit_id for lesson in engine.lessons[:2]]
        summary = engine.run(cfg, selected)
        assert summary["completed"] == 2 and not summary["aborted"]
        assert not fake_obs.active
        store = ProgressStore(cfg.output_path(engine.base), engine.browser.page.url, engine.lessons)
        assert all(store.completed(lesson) for lesson in engine.lessons[:2])
        assert len(list(store.directory.glob("*/*.mp4"))) == 2
        calls = sum(name == "StartRecord" for name, _ in fake_obs.requests)
        summary = engine.run(cfg, selected)
        assert summary["skipped"] == 2 and summary["completed"] == 0
        assert sum(name == "StartRecord" for name, _ in fake_obs.requests) == calls
        entry = store.data["lessons"][selected[0]]
        (store.directory / entry["file"]).unlink()
        summary = engine.run(cfg, [selected[0]])
        assert summary["completed"] == 1 and summary["skipped"] == 0
        assert not fake_obs.active
    finally:
        engine.close()


def test_reading_material_is_skipped_and_batch_continues(cfg, tmp_path, fake_obs, browser_factory):
    cfg = replace(cfg, obs_port=fake_obs.port, obs_password=fake_obs.password)
    events = []
    engine = RecordingEngine(tmp_path / "app", events.append, browser_factory=browser_factory)
    try:
        engine.open_browser(cfg)
        engine.read_catalog(cfg)
        material = next(x for x in engine.lessons if x.unit_id == "unit7777")
        following_video = next(x for x in engine.lessons if x.unit_id == "unit4245")
        selected = [material.unit_id, following_video.unit_id]
        summary = engine.run(cfg, selected)
        assert summary["materials"] == 1 and summary["completed"] == 1
        assert not summary["aborted"] and not fake_obs.active
        assert sum(name == "StartRecord" for name, _ in fake_obs.requests) == 1
        store = ProgressStore(cfg.output_path(engine.base), engine.browser.page.url, engine.lessons)
        assert store.non_video(material) and store.completed(following_video)
        assert any(e.get("status") == "자료 · 건너뜀" for e in events if e["type"] == "lesson")

        calls = sum(name == "StartRecord" for name, _ in fake_obs.requests)
        summary = engine.run(cfg, selected)
        assert summary["materials"] == 1 and summary["skipped"] == 1 and summary["completed"] == 0
        assert sum(name == "StartRecord" for name, _ in fake_obs.requests) == calls
    finally:
        engine.close()


def test_material_skip_engine_flow_without_real_browser(tmp_path):
    material = Lesson("unit7777", "읽기 자료", "대목차", 0, 0, 0)
    video_lesson = Lesson("unit4245", "영상 강의", "대목차", 0, 1, 1)

    class Mouse:
        def move(self, *_):
            pass

    class Page:
        url = "https://example.test/course"
        mouse = Mouse()

        def bring_to_front(self):
            pass

    class Video:
        def prepare(self, *_):
            return 2.0

        def enter_fullscreen(self, *_):
            pass

        def play(self, *_):
            pass

        def watch(self, _cfg, _stop, progress, _health):
            progress({"current": 2.0, "duration": 2.0, "percent": 100.0})
            return 2.0

        def pause(self):
            pass

    class Browser:
        context = True
        page = Page()

        def __init__(self, *_args, **_kwargs):
            self.cfg = None

        def open(self):
            pass

        def catalog(self):
            return [material, video_lesson]

        def select(self, lesson, _stop):
            if lesson.unit_id == material.unit_id:
                raise NonVideoLesson("영상이 없는 자료형 강의")
            return Video()

        def leave_fullscreen(self):
            pass

        def diagnostics(self, *_):
            pass

        def close(self):
            pass

    cfg = Config(
        course_url=Page.url, output_dir=str(tmp_path / "recordings"), playback_only=True,
        min_free_mb=0, retries=0, fullscreen_video=False)
    events = []
    engine = RecordingEngine(tmp_path / "app", events.append, browser_factory=Browser)
    try:
        engine.open_browser(cfg)
        engine.read_catalog(cfg)
        summary = engine.run(cfg, [material.unit_id, video_lesson.unit_id])
        assert summary["materials"] == 1 and summary["completed"] == 1 and not summary["aborted"]
        store = ProgressStore(cfg.output_path(engine.base), Page.url, engine.lessons)
        assert store.non_video(material)
    finally:
        engine.close()


def test_stop_keeps_incomplete_record_and_stops_obs(cfg, tmp_path, fake_obs, browser_factory):
    cfg = replace(cfg, obs_port=fake_obs.port, obs_password=fake_obs.password)
    events = []
    engine = None

    def emit(event):
        events.append(event)
        if event["type"] == "progress" and event["current"] >= 0.35:
            engine.stop_event.set()

    engine = RecordingEngine(tmp_path / "app", emit, browser_factory=browser_factory)
    try:
        engine.open_browser(cfg)
        engine.read_catalog(cfg)
        lesson = engine.lessons[0]
        result = engine.run(cfg, [lesson.unit_id])
        assert result["cancelled"] and result["completed"] == 0
        assert not fake_obs.active
        store = ProgressStore(cfg.output_path(engine.base), engine.browser.page.url, engine.lessons)
        assert not store.completed(lesson)
        assert len(list(store.directory.glob("*/*INCOMPLETE.mp4"))) == 1
        assert store.data["lessons"][lesson.unit_id]["status"] == "cancelled"
    finally:
        engine.close()


def test_playback_test_does_not_claim_recorded_files(cfg, tmp_path, browser_factory):
    cfg = replace(cfg, playback_only=True)
    engine = RecordingEngine(tmp_path / "app", lambda _: None, browser_factory=browser_factory)
    try:
        engine.open_browser(cfg)
        engine.read_catalog(cfg)
        result = engine.run(cfg, [engine.lessons[0].unit_id])
        assert result["completed"] == 1 and not result["aborted"]
        store = ProgressStore(cfg.output_path(engine.base), engine.browser.page.url, engine.lessons)
        assert not store.completed(engine.lessons[0])
        assert not list(store.directory.glob("*/*.mp4"))
    finally:
        engine.close()
