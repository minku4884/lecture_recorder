from __future__ import annotations

import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .common import LectureError, NonVideoLesson, RecorderError, check_stop, finite_number, sleep_checked
from .config import Config
from .storage import Lesson


CATALOG_SCRIPT = """ul => {
    const result = [];
    let section = null, sectionIndex = -1, unitIndex = 0;
    for (const li of ul.children) {
        if (li.matches('li.section')) {
            sectionIndex++; unitIndex = 0;
            section = (li.querySelector('h4')?.textContent || li.textContent).replace(/\\s+/g,' ').trim();
        } else if (section !== null && li.matches('li.unit_line')) {
            // textContent also reads lessons in a collapsed section (innerText can be empty).
            const titleNode = li.querySelector('.unit_title, .unit_name, a');
            const title = (titleNode?.textContent || li.textContent).replace(/\\s+/g,' ').trim();
            result.push({unit_id:li.id, title:title, section_title:section,
                section_index:sectionIndex, unit_index:unitIndex++, global_index:result.length,
                site_done:li.classList.contains('done')});
        }
    }
    return result;
}"""


VIDEO_STATE_SCRIPT = """v => {
    const token = () => globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2);
    v.__lectureRecorderToken ||= token();
    document.__lectureRecorderToken ||= token();
    return {documentToken:document.__lectureRecorderToken, videoToken:v.__lectureRecorderToken,
        source:v.currentSrc || v.src || '',
        current:Number.isFinite(v.currentTime) ? v.currentTime : 0,
        duration:Number.isFinite(v.duration) ? v.duration : 0,
        paused:v.paused, ended:v.ended, seeking:v.seeking, ready:v.readyState,
        error:v.error?.code || 0, rate:v.playbackRate, muted:v.muted, volume:v.volume};
}"""


class VideoRef:
    def __init__(self, frame, locator):
        self.frame, self.video = frame, locator

    def state(self) -> dict:
        try:
            return self.video.evaluate(VIDEO_STATE_SCRIPT, timeout=3000)
        except Exception as error:
            raise LectureError("영상 플레이어가 사라졌거나 교체됐습니다.") from error

    def signature(self, state: dict | None = None) -> tuple:
        state = state or self.state()
        return (state["documentToken"], state["videoToken"], state["source"])

    def pause(self) -> None:
        self.video.evaluate("v => v.pause()", timeout=3000)

    def click_play(self) -> None:
        if not self.state()["paused"]:
            return
        for selector in ('button.plyr__control--overlaid[data-plyr="play"]', 'button[data-plyr="play"]'):
            buttons = self.frame.locator(selector)
            for index in range(buttons.count()):
                button = buttons.nth(index)
                if button.is_visible():
                    try:
                        button.click(timeout=1500)
                        return
                    except Exception:
                        continue
        # The ordinary HTML media API is a final fallback, with rejection propagated.
        self.video.evaluate("async v => { await v.play(); }", timeout=5000)

    def prepare(self, cfg: Config, stop: threading.Event) -> float:
        """Load metadata, pause, then seek to zero BEFORE starting OBS."""
        deadline = time.monotonic() + cfg.load_timeout
        try:
            self.video.evaluate("""v => {
                v.loop = false; v.playbackRate = 1; v.muted = true;
                v.addEventListener('loadedmetadata', () => v.pause(), {once:true});
            }""", timeout=3000)
            warmed = False
            while time.monotonic() < deadline:
                check_stop(stop)
                state = self.state()
                if state["error"]:
                    raise LectureError(f"영상 로딩 오류 (media code {state['error']}).")
                if state["ready"] >= 1 and state["duration"] > 0:
                    break
                if not warmed:
                    self.click_play()
                    warmed = True
                sleep_checked(0.2, stop)
            else:
                raise LectureError("영상 길이를 읽지 못했습니다. 강의 접근/네트워크 상태를 확인하세요.")
            self.pause()
            self.video.evaluate("v => { v.currentTime = 0; v.playbackRate = 1; }", timeout=3000)
            seek_deadline = time.monotonic() + 10
            stable = 0
            while time.monotonic() < seek_deadline:
                check_stop(stop)
                state = self.state()
                if state["current"] <= 0.08 and not state["seeking"] and state["paused"]:
                    stable += 1
                    if stable >= 3:
                        return state["duration"]
                else:
                    stable = 0
                    self.pause()
                    if not state["seeking"]:
                        self.video.evaluate("v => { v.currentTime = 0; }", timeout=3000)
                sleep_checked(0.15, stop)
            raise LectureError("영상을 0초로 되돌리지 못해 녹화를 시작하지 않았습니다.")
        except (LectureError, RecorderError):
            raise
        except Exception as error:
            raise LectureError("영상 준비에 실패했습니다. 브라우저에서 정상 재생되는지 확인하세요.") from error

    def play(self, cfg: Config, stop: threading.Event) -> None:
        check_stop(stop)
        try:
            self.video.evaluate("(v, volume) => { v.muted = false; v.volume = volume; v.playbackRate = 1; }",
                                cfg.volume, timeout=3000)
            self.click_play()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                check_stop(stop)
                state = self.state()
                if state["current"] > 0.03 and (not state["paused"] or state["ended"]):
                    if state["muted"] or state["volume"] <= 0:
                        raise LectureError("영상 소리가 음소거되어 있습니다.")
                    return
                sleep_checked(0.15, stop)
            raise LectureError("재생 버튼을 눌렀지만 영상 재생이 시작되지 않았습니다.")
        except RecorderError:
            raise
        except Exception as error:
            raise LectureError("영상 자동 재생에 실패했습니다.") from error

    def enter_fullscreen(self, log: Callable[[str], None]) -> None:
        try:
            button = self.frame.locator('button[data-plyr="fullscreen"]')
            if button.count() and button.first.is_visible():
                active = self.frame.evaluate("!!document.fullscreenElement")
                if not active:
                    button.first.click(timeout=1500)
        except Exception:
            log("전체 화면 버튼을 사용할 수 없어 현재 브라우저 화면으로 녹화합니다.")

    def leave_fullscreen(self) -> None:
        try:
            self.frame.evaluate("async () => { if(document.fullscreenElement) await document.exitFullscreen(); }")
        except Exception:
            pass

    def watch(self, cfg: Config, stop: threading.Event, on_progress: Callable[[dict], None],
              health: Callable[[], None] = lambda: None) -> float:
        state = self.state()
        identity = self.signature(state)
        initial_duration = state["duration"]
        started = time.monotonic()
        last_progress, last_current = started, state["current"]
        next_health, resume_count, final_stable = started, 0, None
        while True:
            check_stop(stop)
            now, state = time.monotonic(), self.state()
            if self.signature(state) != identity:
                raise LectureError("재생 중 다른 영상으로 전환되었습니다. 현재 녹화는 미완료로 보관합니다.")
            current, duration = finite_number(state["current"]), finite_number(state["duration"])
            on_progress({"current": current, "duration": duration,
                         "percent": min(100, current / duration * 100) if duration > 0 else 0})
            if state["error"]:
                raise LectureError(f"영상 재생 오류 (media code {state['error']}).")
            if state["ended"] and current > 0:
                return duration
            # Only a paused, stable LAST FRAME qualifies when a player omits 'ended'.
            # Do not stop at duration - 0.5; that truncates the last part of every lecture.
            if duration > 0 and current >= duration - 0.001 and state["paused"] and not state["seeking"]:
                final_stable = final_stable or now
                if now - final_stable >= 1.2:
                    return duration
            else:
                final_stable = None
            if current > last_current + 0.04:
                last_current, last_progress, resume_count = current, now, 0
            elif current < last_current - 1:
                raise LectureError("재생 위치가 뒤로 이동했습니다. 완전한 영상 저장을 위해 다시 녹화합니다.")
            if abs(state["rate"] - 1) > 0.01:
                raise LectureError("재생 속도가 1배속에서 변경되었습니다.")
            if state["paused"] and final_stable is None and now - last_progress > 2 and resume_count < 3:
                try:
                    self.click_play()
                except Exception:
                    pass
                resume_count += 1
            if now - last_progress > cfg.stall_timeout:
                raise LectureError(f"영상 진행이 {cfg.stall_timeout:g}초 동안 멈췄습니다.")
            limit = min(cfg.max_lesson_seconds, max(initial_duration, duration) + cfg.stall_timeout * 3 + 120)
            if now - started > limit:
                raise LectureError("강의 최대 재생 대기 시간을 초과했습니다.")
            if now >= next_health:
                health()
                next_health = now + 5
            sleep_checked(0.3, stop)


class CourseBrowser:
    def __init__(self, cfg: Config, base: Path, log: Callable[[str], None], *, headless: bool = False,
                 executable_path: str | None = None):
        self.cfg, self.base, self.log, self.headless = cfg, base, log, headless
        self.executable_path = executable_path
        self.playwright = self.context = self.page = self.current_video = None
        self.selection_attempted = False
        self.confirmed_unit = None
        self.confirmed_tokens = None
        self.confirmed_signature = None

    def _launch_arguments(self, platform: str | None = None) -> dict:
        arguments = {"user_data_dir": str(self.base / "browser_profile"),
                     "headless": self.headless, "no_viewport": True,
                     "args": ["--start-maximized", "--autoplay-policy=no-user-gesture-required"]}
        # Playwright adds --no-sandbox by default. Chrome warns about it on Windows,
        # where it is unnecessary and weakens the normal browser sandbox.
        if (platform or os.name) == "nt":
            arguments["ignore_default_args"] = ["--no-sandbox"]
        if self.executable_path:
            arguments["executable_path"] = self.executable_path
        elif self.cfg.browser != "chromium":
            arguments["channel"] = self.cfg.browser
        return arguments

    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        self.close()
        self.cfg.validate()
        self.playwright = sync_playwright().start()
        arguments = self._launch_arguments()
        try:
            try:
                self.context = self.playwright.chromium.launch_persistent_context(**arguments)
            except Exception:
                if self.cfg.browser == "chromium":
                    raise
                self.log("선택한 브라우저를 열 수 없어 설치된 Chromium으로 다시 시도합니다.")
                arguments.pop("channel", None)
                self.context = self.playwright.chromium.launch_persistent_context(**arguments)
            self.context.set_default_timeout(8000)
            self.context.set_default_navigation_timeout(40000)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            response = self.page.goto(self.cfg.course_url, wait_until="domcontentloaded")
            body = ""
            if response and response.status >= 400:
                try:
                    body = self.page.locator("body").inner_text(timeout=2000)
                except Exception:
                    pass
            if "This Course can not be taken" in body:
                parsed = urlsplit(self.cfg.course_url)
                login_start = f"{parsed.scheme}://{parsed.netloc}/"
                self.page.goto(login_start, wait_until="domcontentloaded")
                self.log("이 브라우저에는 로그인 정보가 없습니다. 로그인한 뒤 강의 주소로 다시 이동하세요.")
            else:
                self.log("브라우저를 열었습니다. 로그인한 뒤 왼쪽 강의 목차가 보이는 화면으로 이동하세요.")
        except Exception as error:
            self.close()
            raise RecorderError("브라우저를 열지 못했습니다. 설치.bat 실행 여부, 강의 주소, 이미 실행 중인 프로그램을 확인하세요.") from error

    def catalog(self) -> list[Lesson]:
        # A login or course link can open a new tab. Pick the tab that contains the real timeline.
        for page in reversed(self.context.pages):
            try:
                root = page.locator(self.cfg.timeline_selector)
                if not root.count():
                    continue
                self.page = page
                raw = root.first.evaluate(CATALOG_SCRIPT)
                lessons = [Lesson(**item) for item in raw]
                if not lessons:
                    continue
                identifiers = [lesson.unit_id for lesson in lessons]
                if any(not re.fullmatch(r"unit[\w-]+", identifier) for identifier in identifiers):
                    raise RecorderError("강의 고유 ID를 읽지 못했습니다. unitXXXX 구조를 확인하세요.")
                if len(set(identifiers)) != len(identifiers):
                    raise RecorderError("강의 ID가 중복됩니다. 현재 강의 화면을 다시 확인하세요.")
                if any(not lesson.title for lesson in lessons):
                    raise RecorderError("제목이 없는 강의가 있습니다. 목차 로딩이 끝난 뒤 다시 읽으세요.")
                self.page.bring_to_front()
                self.log(f"강의 목록: {len(set(x.section_index for x in lessons))}개 대목차 / {len(lessons)}개 강의")
                return lessons
            except RecorderError:
                raise
            except Exception:
                continue
        raise RecorderError("강의 목차를 찾지 못했습니다. 로그인 후 왼쪽 목차가 보이는 화면에서 '강의 목록 읽기'를 누르세요.")

    def videos(self) -> list[VideoRef]:
        result = []
        for frame in self.page.frames:
            try:
                if frame.is_detached():
                    continue
                candidates = frame.locator(self.cfg.video_selector)
                for index in range(candidates.count()):
                    locator = candidates.nth(index)
                    if locator.is_visible():
                        result.append(VideoRef(frame, locator))
            except Exception:
                continue
        return result

    def _currently_selected(self, lesson: Lesson) -> bool:
        return self.page.evaluate("""({id, title}) => {
            const unit = document.getElementById(id);
            if (unit && (['active','current','selected'].some(x => unit.classList.contains(x)) ||
                unit.matches('[aria-current="true"],[aria-current="page"]') ||
                unit.querySelector('[aria-current="true"],[aria-current="page"]'))) return true;
            const normalized = s => s.replace(/\\s+/g,'').trim();
            return [...document.querySelectorAll('.course_content_panel h1, .course_content_panel h2, .course_content_panel h3, .course_content_panel h4, .course_content_panel .unit_title')]
                .some(x => normalized(x.textContent) === normalized(title));
        }""", {"id": lesson.unit_id, "title": lesson.title})

    def _activate_unit(self, unit, section) -> None:
        """Click a lesson even when the site has collapsed or hidden the timeline."""
        try:
            if not unit.is_visible():
                heading = section.locator("h4")
                (heading.first if heading.count() else section).click(timeout=2500)
            unit.wait_for(state="visible", timeout=2500)
            unit.scroll_into_view_if_needed(timeout=2500)
            links = unit.locator("a")
            visible_link = next((links.nth(i) for i in range(links.count()) if links.nth(i).is_visible()), None)
            (visible_link if visible_link is not None else unit).click(timeout=4000)
            return
        except Exception:
            pass
        # The course sidebar can be fully folded by the site. A DOM click still uses
        # the site's own event handler without forcing the user to keep the panel open.
        unit.evaluate("""u => {
            const target = [...u.querySelectorAll('a, button, [role="button"]')]
                .find(x => !x.disabled) || u;
            target.click();
        }""", timeout=3000)

    def select(self, lesson: Lesson, stop: threading.Event) -> VideoRef:
        self.leave_fullscreen()
        check_stop(stop)
        if self.page.is_closed():
            raise RecorderError("강의 브라우저가 닫혔습니다.")
        section = self.page.locator(self.cfg.timeline_selector + " > li.section").nth(lesson.section_index)
        unit = self.page.locator(f'[id="{lesson.unit_id}"]')
        try:
            baseline = {video.signature() for video in self.videos()}
            previous_identities = set(baseline)
            if self.confirmed_signature:
                previous_identities.add(self.confirmed_signature)
            selected_in_ui = self._currently_selected(lesson)
            already_selected = selected_in_ui and (
                not self.selection_attempted or
                (self.confirmed_unit == lesson.unit_id and any(
                    identity[:2] == self.confirmed_tokens for identity in baseline))
            )
            self.selection_attempted = True
            # Explicitly restart only an already selected lesson; never accept an old different player.
            if already_selected:
                for video in self.videos():
                    video.pause()
            self._activate_unit(unit, section)
        except RecorderError:
            raise
        except Exception as error:
            raise LectureError(f"강의 목차를 클릭하지 못했습니다: {lesson.unit_id}") from error
        deadline = time.monotonic() + self.cfg.load_timeout
        candidate_identity, stable, clicked_at = None, 0, time.monotonic()
        material_since = None
        while time.monotonic() < deadline:
            check_stop(stop)
            now = time.monotonic()
            visible_videos = self.videos()
            acceptable = []
            for video in visible_videos:
                try:
                    identity = video.signature()
                    changed = identity not in previous_identities
                    same_selected = already_selected and now - clicked_at > 1
                    if changed or same_selected:
                        acceptable.append((identity, video))
                except LectureError:
                    continue
            if len(acceptable) == 1:
                identity, video = acceptable[0]
                stable = stable + 1 if identity == candidate_identity else 1
                candidate_identity = identity
                if stable >= 3:
                    self.current_video = video
                    self.confirmed_unit = lesson.unit_id
                    self.confirmed_tokens = identity[:2]
                    self.confirmed_signature = identity
                    return video
            else:
                stable = 0
            try:
                selected_without_video = self._currently_selected(lesson) and not visible_videos
            except Exception:
                selected_without_video = False
            if selected_without_video:
                material_since = material_since or now
                if now - material_since >= min(self.cfg.material_timeout, self.cfg.load_timeout):
                    self.current_video = None
                    raise NonVideoLesson(f"영상이 없는 자료형 강의라 자동으로 건너뜁니다: {lesson.title}")
            else:
                material_since = None
            sleep_checked(0.2, stop)
        try:
            if self._currently_selected(lesson) and not self.videos():
                self.current_video = None
                raise NonVideoLesson(f"영상이 없는 자료형 강의라 자동으로 건너뜁니다: {lesson.title}")
        except NonVideoLesson:
            raise
        except Exception:
            pass
        raise LectureError("새 강의의 플레이어 전환을 확인하지 못했습니다. 이전 영상을 잘못 녹화하지 않도록 중단합니다.")

    def leave_fullscreen(self) -> None:
        if self.current_video:
            self.current_video.leave_fullscreen()
        if self.page and not self.page.is_closed():
            try:
                self.page.evaluate("async () => { if(document.fullscreenElement) await document.exitFullscreen(); }")
            except Exception:
                pass

    def diagnostics(self, folder: Path, lesson: Lesson) -> None:
        # Keep just the visible screenshot; no cookies, page HTML, or signed media URLs are dumped.
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(folder / f"{lesson.unit_id}_{uuid.uuid4().hex[:8]}.png"))
        except Exception:
            pass

    def close(self) -> None:
        self.current_video = None
        self.selection_attempted = False
        self.confirmed_unit = self.confirmed_tokens = self.confirmed_signature = None
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        self.context = self.page = None
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.playwright = None
