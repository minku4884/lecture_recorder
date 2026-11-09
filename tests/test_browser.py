import threading
import time
from dataclasses import replace

import pytest

from recorder.common import Cancelled, LectureError, NonVideoLesson
from recorder.browser import CourseBrowser, VideoRef
from recorder.config import Config
from recorder.storage import Lesson


def test_hidden_lessons_are_read_in_site_order(browser):
    lessons = browser.catalog()
    assert [x.unit_id for x in lessons] == ["unit4553", "unit4182", "unit7777", "unit4245", "unit9999"]
    assert [x.title for x in lessons][:2] == ["첫 번째 강의", "두 번째 강의"]
    assert lessons[1].section_title == "Project 1" and lessons[1].unit_index == 0
    assert lessons[0].site_done and lessons[1].site_done and not lessons[2].site_done


def test_collapsed_timeline_can_still_select_the_next_video(browser, cfg):
    lessons, stop = browser.catalog(), threading.Event()
    browser.page.locator(".course_timeline").evaluate("e => e.style.display = 'none'")
    video = browser.select(next(x for x in lessons if x.unit_id == "unit4245"), stop)
    assert abs(video.prepare(cfg, stop) - 2.8) < 0.15


def test_non_video_material_is_detected_and_next_video_still_works(browser, cfg):
    lessons, stop = browser.catalog(), threading.Event()
    material = next(x for x in lessons if x.unit_id == "unit7777")
    with pytest.raises(NonVideoLesson, match="자료형"):
        browser.select(material, stop)
    video = browser.select(next(x for x in lessons if x.unit_id == "unit4245"), stop)
    assert abs(video.prepare(cfg, stop) - 2.8) < 0.15


def test_windows_launch_keeps_chrome_sandbox(tmp_path):
    course = CourseBrowser(Config(course_url="https://example.test/course"), tmp_path, lambda _: None)
    arguments = course._launch_arguments("nt")
    assert arguments["ignore_default_args"] == ["--no-sandbox"]


def test_hidden_timeline_falls_back_to_dom_click(tmp_path):
    class Heading:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def click(self, **_):
            raise RuntimeError("timeline is hidden")

    class Section:
        def locator(self, _):
            return Heading()

    class Unit:
        dom_clicked = False

        def is_visible(self):
            return False

        def wait_for(self, **_):
            raise RuntimeError("hidden")

        def evaluate(self, *_args, **_kwargs):
            self.dom_clicked = True

    course = CourseBrowser(Config(course_url="https://example.test/course"), tmp_path, lambda _: None)
    unit = Unit()
    course._activate_unit(unit, Section())
    assert unit.dom_clicked


def test_selected_page_without_video_is_classified_as_material(tmp_path):
    class Locator:
        def nth(self, _):
            return self

    class Page:
        def is_closed(self):
            return False

        def locator(self, _):
            return Locator()

    config = Config(course_url="https://example.test/course", load_timeout=0.1, material_timeout=0.02)
    course = CourseBrowser(config, tmp_path, lambda _: None)
    course.page = Page()
    course.leave_fullscreen = lambda: None
    course.videos = lambda: []
    course._currently_selected = lambda _lesson: True
    course._activate_unit = lambda _unit, _section: None
    material = Lesson("unit7777", "읽기 자료", "대목차", 0, 0, 0)
    with pytest.raises(NonVideoLesson, match="자료형"):
        course.select(material, threading.Event())


def test_nested_cross_origin_player_and_full_playback(browser, cfg):
    lesson = browser.catalog()[0]
    stop = threading.Event()
    video = browser.select(lesson, stop)
    duration = video.prepare(cfg, stop)
    assert abs(duration - 1.8) < 0.15
    assert video.state()["paused"] and video.state()["current"] <= 0.08
    video.play(cfg, stop)
    progress = []
    video.watch(cfg, stop, progress.append)
    final = video.state()
    assert final["ended"] and final["current"] >= duration - 0.01
    assert progress and max(x["percent"] for x in progress) > 99


def test_delayed_transition_uses_the_new_video(browser, cfg):
    lessons, stop = browser.catalog(), threading.Event()
    initial = browser.select(lessons[0], stop)
    initial.prepare(cfg, stop)
    old_signature = initial.signature()
    before = time.monotonic()
    new = browser.select(lessons[1], stop)
    assert time.monotonic() - before >= 0.65
    assert new.signature() != old_signature
    duration = new.prepare(cfg, stop)
    assert abs(duration - 2.8) < 0.15
    assert new.state()["source"].endswith("/two.webm")


def test_stale_video_is_rejected_on_first_attempt_and_retry(browser, cfg):
    lessons, stop = browser.catalog(), threading.Event()
    browser.select(lessons[0], stop).prepare(cfg, stop)
    browser.cfg = replace(cfg, load_timeout=1.4)
    # The UI title and active class change, but the iframe never changes.
    for _ in range(2):
        with pytest.raises(LectureError, match="플레이어 전환"):
            browser.select(lessons[-1], stop)


def test_stalled_playback_is_a_failure(browser, cfg):
    stop = threading.Event()
    video = browser.select(browser.catalog()[0], stop)
    video.prepare(cfg, stop)
    video.play(cfg, stop)
    video.pause()
    with pytest.raises(LectureError, match="멈췄"):
        video.watch(replace(cfg, stall_timeout=0.5), stop, lambda _: None)


def test_cancelled_playback_never_reports_completion(browser, cfg):
    stop = threading.Event()
    video = browser.select(browser.catalog()[0], stop)
    video.prepare(cfg, stop)
    video.play(cfg, stop)
    stop.set()
    with pytest.raises(Cancelled):
        video.watch(cfg, stop, lambda _: None)


def test_last_30ms_are_not_mistaken_for_completion():
    video = VideoRef(None, None)
    video.state = lambda: {"documentToken": "doc", "videoToken": "video", "source": "clip",
        "current": 9.97, "duration": 10, "paused": True, "ended": False,
        "seeking": False, "error": 0, "rate": 1}
    with pytest.raises(LectureError, match="멈췄"):
        video.watch(Config(stall_timeout=0.5), threading.Event(), lambda _: None)
