from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from recorder.common import RecorderError, clock
from recorder.config import Config
from recorder.engine import RecordingEngine
from recorder.instance import SingleInstance


def cli(base: Path, config_path: Path, args) -> int:
    def emit(event):
        if event["type"] == "log":
            print(event["message"], flush=True)
        elif event["type"] == "progress":
            print(f"\r{clock(event['current'])} / {clock(event['duration'])}  {event['percent']:5.1f}%", end="", flush=True)
        elif event["type"] == "summary":
            print(f"\n완료 {event['completed']} / 기존 파일 {event['skipped']} / 실패 {event['failed']}")

    cfg = Config.load(config_path)
    if args.url:
        cfg.course_url = args.url
    if args.playback_only:
        cfg.playback_only = True
    if args.no_auto_scene:
        cfg.auto_scene = False
    cfg.validate(need_url=not args.check_obs)
    engine = RecordingEngine(base, emit)

    def stop_signal(*_):
        engine.stop_event.set()
        print("\n중지 요청: 현재 파일을 보관하고 종료합니다.", flush=True)

    signal.signal(signal.SIGINT, stop_signal)
    try:
        if args.check_obs:
            engine.test_obs(cfg)
            return 0
        engine.open_browser(cfg)
        input("브라우저에서 로그인하고 강의 목차 화면에 들어간 뒤 Enter… ")
        engine.read_catalog(cfg)
        for lesson in engine.lessons:
            print(f"{lesson.global_index + 1:03d} {lesson.section_title} / {lesson.title} [{lesson.unit_id}]")
        if not args.yes:
            input("목록을 확인하고 Enter를 누르면 전체 강의 처리를 시작합니다… ")
        selected = [lesson.unit_id for lesson in engine.lessons]
        if args.limit:
            selected = selected[:args.limit]
        summary = engine.run(cfg, selected)
        return 1 if summary["aborted"] or summary["failed"] else (130 if summary["cancelled"] else 0)
    finally:
        engine.close()


def main() -> int:
    base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="강의 목차 자동 순회 + 강의별 OBS 녹화")
    parser.add_argument("--cli", action="store_true", help="콘솔 모드")
    parser.add_argument("--check-obs", action="store_true", help="OBS 연결만 검사")
    parser.add_argument("--config", type=Path, default=base / "config.json")
    parser.add_argument("--url", help="강의 페이지 주소 (콘솔 모드)")
    parser.add_argument("--playback-only", action="store_true", help="녹화 없이 재생 테스트")
    parser.add_argument("--no-auto-scene", action="store_true", help="기존 OBS 장면 사용")
    parser.add_argument("--limit", type=int, help="앞에서부터 지정 개수의 강의만 처리")
    parser.add_argument("--yes", action="store_true", help="콘솔 목록 확인 단계 생략 (최초 로그인 단계는 유지)")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit은 1 이상이어야 합니다.")
    try:
        with SingleInstance(base):
            if args.cli or args.check_obs:
                return cli(base, args.config, args)
            from recorder.gui import launch

            launch(base, args.config)
            return 0
    except ImportError as error:
        print(f"필요한 패키지가 없습니다. 설치.bat를 먼저 실행하세요. ({error.name})", file=sys.stderr)
        return 1
    except RecorderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"실행 실패 ({type(error).__name__}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
