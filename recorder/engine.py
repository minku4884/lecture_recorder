from __future__ import annotations

import logging
import queue
import shutil
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from .browser import CourseBrowser
from .common import Cancelled, KeepAwake, LectureError, NonVideoLesson, RecorderError, check_stop
from .config import Config
from .obs import ObsClient, ObsError, ObsSession
from .storage import Lesson, ProgressStore, check_disk, remux_mp4


class RecordingEngine:
    def __init__(self, base: Path, emit: Callable[[dict], None], *, browser_factory=CourseBrowser,
                 obs_factory=ObsSession):
        self.base, self.emit, self.browser_factory, self.obs_factory = base, emit, browser_factory, obs_factory
        self.browser = None
        self.lessons: list[Lesson] = []
        self.stop_event = threading.Event()
        self.opened_url = ""
        log_dir = base / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.Logger("lecture_recorder")
        handler = logging.FileHandler(log_dir / (datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.logger.addHandler(handler)

    def log(self, message: str) -> None:
        self.logger.info(message)
        self.emit({"type": "log", "message": message})

    def open_browser(self, cfg: Config) -> None:
        cfg.validate()
        if self.browser:
            self.browser.close()
        self.lessons = []
        self.emit({"type": "catalog", "lessons": [], "completed": []})
        self.browser = self.browser_factory(cfg, self.base, self.log)
        self.browser.open()
        self.opened_url = cfg.course_url

    def read_catalog(self, cfg: Config) -> None:
        if not self.browser or not self.browser.context:
            raise RecorderError("먼저 '브라우저 열기'를 누르세요.")
        self.browser.cfg = cfg
        self.lessons = self.browser.catalog()
        store = ProgressStore(cfg.output_path(self.base), self.browser.page.url, self.lessons)
        completed = [lesson.unit_id for lesson in self.lessons if store.completed(lesson)]
        materials = [lesson.unit_id for lesson in self.lessons if store.non_video(lesson)]
        self.emit({"type": "catalog", "lessons": [asdict(x) for x in self.lessons],
                   "completed": completed, "materials": materials})

    def test_obs(self, cfg: Config) -> None:
        cfg.validate(need_url=False)
        client = ObsClient(cfg)
        try:
            client.connect()
            version, status = client.request("GetVersion"), client.request("GetRecordStatus")
            self.log("OBS 연결 정상 · 버전 " + version.get("obsVersion", "") +
                     (" · 현재 녹화 중" if status["outputActive"] else " · 녹화 대기"))
            self.emit({"type": "obs_ok"})
        finally:
            client.close()

    def _lesson(self, cfg: Config, lesson: Lesson, store: ProgressStore, obs: ObsSession | None) -> None:
        video, complete, archived = None, False, None
        try:
            self.emit({"type": "lesson", "unit_id": lesson.unit_id, "title": lesson.title,
                       "section": lesson.section_title, "status": "영상 준비"})
            video = self.browser.select(lesson, self.stop_event)
            duration = video.prepare(cfg, self.stop_event)
            self.browser.page.bring_to_front()
            if cfg.fullscreen_video:
                video.enter_fullscreen(self.log)
            # Move the pointer out of the playback controls, then start recording BEFORE play.
            try:
                self.browser.page.mouse.move(0, 0)
            except Exception:
                pass
            if obs:
                obs.start()
                store.mark(lesson, "recording")
            check_stop(self.stop_event)
            self.emit({"type": "lesson", "unit_id": lesson.unit_id,
                       "status": "녹화 중" if obs else "재생 테스트"})
            video.play(cfg, self.stop_event)

            def health() -> None:
                if obs:
                    obs.assert_recording()
                    check_disk(store.directory, cfg.min_free_mb)
                    check_disk(self.record_directory, cfg.min_free_mb)

            def progress(state: dict) -> None:
                self.emit({"type": "progress", "unit_id": lesson.unit_id, **state})

            duration = video.watch(cfg, self.stop_event, progress, health)
            video.pause()
            if obs:
                # Let the OS audio/capture buffers drain after the actual media end.
                time.sleep(0.35)
                obs.assert_recording()
                source = obs.stop()
                archived = store.archive(source, lesson, True, self.stop_event)
                # Commit the original file before optional remux, so a failed conversion never loses progress.
                store.mark(lesson, "complete", archived, duration)
                complete = True
                if cfg.remux_mp4 and self.ffmpeg and archived.suffix.lower() != ".mp4":
                    self.emit({"type": "lesson", "unit_id": lesson.unit_id, "status": "MP4 변환"})
                    try:
                        converted = remux_mp4(archived, self.ffmpeg, self.stop_event)
                        store.mark(lesson, "complete", converted, duration)
                        archived = converted
                    except Cancelled:
                        self.log("MP4 변환을 중지했습니다. 완료된 원본 녹화 파일은 보존했습니다.")
                    except RecorderError as error:
                        self.log(str(error))
                self.log(f"저장 완료: {archived}")
            else:
                complete = True
                self.log("재생 테스트 완료: " + lesson.title)
            self.emit({"type": "lesson", "unit_id": lesson.unit_id,
                       "status": "저장 완료" if obs else "테스트 완료"})
        except BaseException as error:
            if video:
                try:
                    video.pause()
                except Exception:
                    pass
            partial = None
            if obs and obs.owned_record:
                try:
                    source = obs.stop()
                    partial = store.archive(source, lesson, False, self.stop_event)
                    self.log(f"미완료 파일 보관: {partial}")
                except Exception as cleanup_error:
                    self.log("OBS 종료/미완료 파일 보관 오류: " + str(cleanup_error))
                    # An unresolved recording must halt the batch, including when continue_on_error is set.
                    if not isinstance(error, Cancelled):
                        raise ObsError("OBS 녹화 종료 상태가 불확실해 자동화를 중단했습니다.") from error
            if obs and not complete and not isinstance(error, NonVideoLesson):
                store.mark(lesson, "cancelled" if isinstance(error, Cancelled) else "failed",
                           partial, error=str(error))
            raise
        finally:
            self.browser.leave_fullscreen()

    def run(self, cfg: Config, selected_ids: list[str]) -> dict:
        summary = {"type": "summary", "completed": 0, "skipped": 0, "materials": 0, "failed": 0,
                   "cancelled": False, "aborted": False, "total": 0, "playback_only": cfg.playback_only}
        obs = None
        try:
            cfg.validate()
            if not self.browser or not self.lessons:
                raise RecorderError("브라우저를 열고 강의 목록을 먼저 읽으세요.")
            if cfg.course_url != self.opened_url:
                raise RecorderError("강의 주소가 변경되었습니다. '브라우저 열기'부터 다시 실행하세요.")
            self.browser.cfg = cfg
            selected_set = set(selected_ids)
            selected = [lesson for lesson in self.lessons if lesson.unit_id in selected_set]
            if not selected:
                raise RecorderError("녹화할 강의를 하나 이상 선택하세요.")
            summary["total"] = len(selected)
            output = cfg.output_path(self.base)
            output.mkdir(parents=True, exist_ok=True)
            check_disk(output, cfg.min_free_mb)
            store = ProgressStore(output, self.browser.page.url, self.lessons)
            self.ffmpeg = cfg.ffmpeg_path or shutil.which("ffmpeg")
            if cfg.remux_mp4 and not self.ffmpeg and not cfg.playback_only:
                self.log("FFmpeg가 없어 OBS에서 선택한 원래 형식으로 저장합니다. OBS를 MP4로 설정하면 MP4로 저장됩니다.")
            pending = []
            for lesson in selected:
                if cfg.resume and store.non_video(lesson):
                    summary["materials"] += 1
                    self.emit({"type": "lesson", "unit_id": lesson.unit_id, "status": "자료 · 건너뜀"})
                elif cfg.resume and not cfg.playback_only and store.completed(lesson):
                    summary["skipped"] += 1
                    self.emit({"type": "lesson", "unit_id": lesson.unit_id, "status": "기존 파일 확인"})
                else:
                    pending.append(lesson)
            if not pending:
                self.log("선택한 항목이 모두 완료되었거나 자료형으로 처리되었습니다.")
                return summary
            if not cfg.playback_only:
                obs = self.obs_factory(cfg, self.base, self.log)
                obs.open()
                self.record_directory = Path(obs.client.request("GetRecordDirectory")["recordDirectory"])
                check_disk(self.record_directory, cfg.min_free_mb)
            self.emit({"type": "recording_started"})
            with KeepAwake():
                for index, lesson in enumerate(pending):
                    check_stop(self.stop_event)
                    self.log(f"[{index + 1}/{len(pending)}] {lesson.section_title} / {lesson.title}")
                    for attempt in range(cfg.retries + 1):
                        check_stop(self.stop_event)
                        try:
                            self._lesson(cfg, lesson, store, obs)
                            summary["completed"] += 1
                            break
                        except NonVideoLesson as error:
                            store.mark(lesson, "non_video", error=str(error))
                            summary["materials"] += 1
                            self.log(str(error))
                            self.emit({"type": "lesson", "unit_id": lesson.unit_id, "status": "자료 · 건너뜀"})
                            break
                        except LectureError as error:
                            self.log(str(error))
                            self.browser.diagnostics(store.directory / "diagnostics", lesson)
                            if attempt < cfg.retries:
                                self.log(f"강의를 0초부터 다시 시도합니다 ({attempt + 1}/{cfg.retries}).")
                                continue
                            summary["failed"] += 1
                            self.emit({"type": "lesson", "unit_id": lesson.unit_id, "status": "실패"})
                            if not cfg.continue_on_error:
                                summary["aborted"] = True
                                self.emit({"type": "error", "message": str(error)})
                                return summary
                        except Cancelled:
                            self.emit({"type": "lesson", "unit_id": lesson.unit_id, "status": "중지"})
                            raise
                        except Exception:
                            summary["failed"] += 1
                            self.emit({"type": "lesson", "unit_id": lesson.unit_id, "status": "실패"})
                            raise
                    self.emit({"type": "counts", **{key: summary[key] for key in
                                                     ("completed", "skipped", "materials", "failed", "total")}})
            self.log("선택한 강의 처리가 끝났습니다.")
            return summary
        except Cancelled:
            summary["cancelled"] = True
            self.log("중지 완료. 완료 파일은 그대로 유지합니다.")
            return summary
        except Exception as error:
            summary["aborted"] = True
            self.log(str(error))
            self.emit({"type": "error", "message": str(error)})
            return summary
        finally:
            if obs:
                try:
                    obs.close()
                except Exception as error:
                    summary["aborted"] = True
                    self.log("OBS 정리 오류: " + str(error))
                    self.emit({"type": "error", "message": "OBS 자동 종료/복원에 실패했습니다. OBS 상태를 직접 확인하세요."})
            self.emit(summary)

    def close(self) -> None:
        if self.browser:
            self.browser.close()
        for handler in self.logger.handlers:
            handler.close()


class Worker:
    """All Playwright/OBS objects stay on one worker thread; Tk never touches them."""

    def __init__(self, base: Path):
        self.events, self.commands = queue.Queue(), queue.Queue()
        self.engine = RecordingEngine(base, self.events.put)
        self.thread = threading.Thread(target=self._loop, name="LectureRecorderWorker", daemon=True)
        self.thread.start()

    def submit(self, command: str, cfg: Config | None = None, selected: list[str] | None = None) -> None:
        if command == "record":
            self.engine.stop_event.clear()
        self.commands.put((command, cfg, selected))

    def stop(self) -> None:
        self.engine.stop_event.set()

    def _loop(self) -> None:
        while True:
            command, cfg, selected = self.commands.get()
            try:
                if command == "open":
                    self.engine.open_browser(cfg)
                elif command == "catalog":
                    self.engine.read_catalog(cfg)
                elif command == "test_obs":
                    self.engine.test_obs(cfg)
                elif command == "record":
                    self.engine.run(cfg, selected or [])
                elif command == "close":
                    self.engine.close()
                    self.events.put({"type": "closed"})
                    return
            except Exception as error:
                self.engine.log(str(error))
                self.events.put({"type": "error", "message": str(error)})
            finally:
                self.events.put({"type": "done", "command": command})
