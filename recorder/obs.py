from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable

from .common import RecorderError, atomic_json
from .config import Config


class ObsError(RecorderError):
    pass


class ObsRequestError(ObsError):
    def __init__(self, request: str, code: int, comment: str):
        self.code = code
        super().__init__(f"OBS {request} 실패 ({code}): {comment}")


def authentication(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsClient:
    """Small synchronous OBS WebSocket 5/RPC 1 client; no media downloading."""

    def __init__(self, cfg: Config, timeout: float = 8):
        self.cfg, self.timeout, self.socket = cfg, timeout, None

    def connect(self) -> None:
        import websocket

        self.close()
        host = "[::1]" if self.cfg.obs_host == "::1" else self.cfg.obs_host
        try:
            self.socket = websocket.create_connection(
                f"ws://{host}:{self.cfg.obs_port}", timeout=self.timeout,
                http_no_proxy=["localhost", "127.0.0.1", "::1"],
            )
            hello = self._receive()
            if hello.get("op") != 0 or hello.get("d", {}).get("rpcVersion", 0) < 1:
                raise ObsError("OBS WebSocket 5가 필요합니다. OBS Studio 28 이상을 사용하세요.")
            identify = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = hello["d"].get("authentication")
            if auth:
                identify["authentication"] = authentication(self.cfg.obs_password, auth["salt"], auth["challenge"])
            self.socket.send(json.dumps({"op": 1, "d": identify}))
            if self._receive().get("op") != 2:
                raise ObsError("OBS 인증에 실패했습니다. WebSocket 비밀번호를 확인하세요.")
        except ObsError:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise ObsError("OBS 연결/인증 실패: OBS 실행 여부, WebSocket 활성화, 포트와 비밀번호를 확인하세요.") from error

    def _receive(self) -> dict:
        message = self.socket.recv()
        if not message:
            raise ObsError("OBS 연결이 종료됐습니다. 비밀번호와 OBS 상태를 확인하세요.")
        return json.loads(message)

    def request(self, request: str, data: dict | None = None) -> dict:
        if self.socket is None:
            raise ObsError("OBS에 연결되어 있지 않습니다.")
        identifier = str(uuid.uuid4())
        payload = {"requestType": request, "requestId": identifier, "requestData": data or {}}
        deadline = time.monotonic() + self.timeout
        try:
            self.socket.send(json.dumps({"op": 6, "d": payload}))
            while time.monotonic() < deadline:
                self.socket.settimeout(max(0.01, deadline - time.monotonic()))
                message = self._receive()
                if message.get("op") != 7 or message.get("d", {}).get("requestId") != identifier:
                    continue
                response = message["d"]
                status = response["requestStatus"]
                if not status["result"]:
                    raise ObsRequestError(request, status.get("code", 0), status.get("comment", ""))
                return response.get("responseData", {})
            raise ObsError(f"OBS 응답 시간 초과: {request}")
        except ObsError:
            raise
        except Exception as error:
            raise ObsError(f"OBS 통신 실패: {request}") from error
        finally:
            if self.socket:
                self.socket.settimeout(self.timeout)

    def close(self) -> None:
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None


class ObsSession:
    """Record ownership plus reversible automatic display/audio scene configuration."""

    def __init__(self, cfg: Config, base: Path, log: Callable[[str], None]):
        self.cfg, self.log = cfg, log
        self.client = ObsClient(cfg)
        self.journal_path = base / "runtime" / "obs_restore.json"
        self.journal: dict | None = None
        self.owned_record = False
        self.last_path: Path | None = None
        self.is_windows = os.name == "nt"

    def open(self) -> None:
        self.client.connect()
        if self.client.request("GetRecordStatus")["outputActive"]:
            raise ObsError("OBS에서 이미 녹화 중입니다. 현재 녹화를 종료한 뒤 시작하세요.")
        if self.journal_path.exists():
            try:
                self.journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
            except Exception as error:
                raise ObsError("이전 OBS 복원 기록을 읽을 수 없습니다. runtime/obs_restore.json을 확인하세요.") from error
            if self.journal.get("server") != [self.cfg.obs_host, int(self.cfg.obs_port)]:
                raise ObsError("이전 OBS 설정 복원 기록이 다른 포트를 사용합니다. 이전 OBS 포트로 연결해 복원하세요.")
            if self.client.request("GetStreamStatus")["outputActive"]:
                raise ObsError("이전 OBS 설정을 복원해야 합니다. 방송을 종료한 뒤 다시 연결하세요.")
            self.restore()
            self.log("이전 실행의 OBS 설정을 복원했습니다.")
        self.log("OBS 연결 완료: " + self.client.request("GetVersion").get("obsVersion", ""))
        if self.cfg.auto_scene:
            if self.client.request("GetStreamStatus")["outputActive"]:
                raise ObsError("방송 중에는 자동 장면을 구성할 수 없습니다. 방송을 종료하거나 자동 장면 옵션을 끄세요.")
            self.setup_scene()

    def _save_journal(self) -> None:
        atomic_json(self.journal_path, self.journal)

    def setup_scene(self) -> None:
        if not self.is_windows:
            raise ObsError("자동 화면/소리 구성은 Windows용입니다. 다른 OS에서는 OBS 장면을 직접 구성하고 옵션을 끄세요.")
        kinds = self.client.request("GetInputKindList", {"unversioned": True})["inputKinds"]
        if "monitor_capture" not in kinds or "wasapi_output_capture" not in kinds:
            raise ObsError("OBS의 Windows 화면/오디오 캡처 소스를 찾지 못했습니다. 자동 장면 옵션을 끄고 수동 구성하세요.")
        scene = "Lecture Recorder " + uuid.uuid4().hex[:8]
        self.journal = {
            "server": [self.cfg.obs_host, int(self.cfg.obs_port)],
            "original_scene": self.client.request("GetCurrentProgramScene")["currentProgramSceneName"],
            "mutes": {}, "scene": scene, "scene_created": False, "inputs": [],
        }
        special = self.client.request("GetSpecialInputs")
        for name in set(value for value in special.values() if isinstance(value, str) and value):
            self.journal["mutes"][name] = self.client.request("GetInputMute", {"inputName": name})["inputMuted"]
        self._save_journal()
        self.client.request("CreateScene", {"sceneName": scene})
        self.journal["scene_created"] = True
        self._save_journal()
        display = scene + " Display"
        settings = self.client.request("GetInputDefaultSettings", {"inputKind": "monitor_capture"})["defaultInputSettings"]
        settings.update(capture_cursor=False)
        self._create_input(scene, display, "monitor_capture", settings)
        item_id = self.client.request("GetSceneItemId", {"sceneName": scene, "sourceName": display})["sceneItemId"]
        video = self.client.request("GetVideoSettings")
        self.client.request("SetSceneItemTransform", {
            "sceneName": scene, "sceneItemId": item_id,
            "sceneItemTransform": {"positionX": 0.0, "positionY": 0.0, "alignment": 5,
                                   "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 0,
                                   "boundsWidth": float(video["baseWidth"]), "boundsHeight": float(video["baseHeight"])},
        })
        audio = scene + " Audio"
        settings = self.client.request("GetInputDefaultSettings", {"inputKind": "wasapi_output_capture"})["defaultInputSettings"]
        settings.update(device_id="default")
        self._create_input(scene, audio, "wasapi_output_capture", settings)
        self.client.request("SetInputMute", {"inputName": audio, "inputMuted": False})
        self.client.request("SetInputVolume", {"inputName": audio, "inputVolumeMul": 1.0})
        self.client.request("SetInputAudioTracks", {"inputName": audio, "inputAudioTracks": {str(i): True for i in range(1, 7)}})
        for name in self.journal["mutes"]:
            self.client.request("SetInputMute", {"inputName": name, "inputMuted": True})
        self.client.request("SetCurrentProgramScene", {"sceneName": scene})
        self.log("OBS 자동 장면 구성: 기본 모니터 + 기본 PC 소리. 마이크는 녹화에서 제외됩니다.")

    def _create_input(self, scene: str, name: str, kind: str, settings: dict) -> None:
        # Journal planned names before sending, so accepted requests can be recovered after a lost response.
        self.journal["inputs"].append(name)
        self._save_journal()
        self.client.request("CreateInput", {"sceneName": scene, "inputName": name,
                                            "inputKind": kind, "inputSettings": settings, "sceneItemEnabled": True})

    def start(self) -> None:
        if self.client.request("GetRecordStatus")["outputActive"]:
            raise ObsError("다른 OBS 녹화가 시작되었습니다. 자동화를 중단합니다.")
        self.last_path = None
        # A lost response may still mean StartRecord was accepted. Retain ownership for cleanup.
        self.owned_record = True
        try:
            self.client.request("StartRecord")
        except ObsRequestError:
            self.owned_record = False
            raise
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = self.client.request("GetRecordStatus")
            if status["outputActive"] and not status.get("outputPaused", False):
                return
            time.sleep(0.1)
        raise ObsError("OBS 녹화 시작 상태를 확인하지 못했습니다.")

    def assert_recording(self) -> None:
        state = self.client.request("GetRecordStatus")
        if not self.owned_record or not state["outputActive"] or state.get("outputPaused", False):
            raise ObsError("OBS 녹화가 중단/일시정지됐습니다. 이 강의는 완료 처리하지 않습니다.")

    def stop(self) -> Path | None:
        if not self.owned_record:
            return None
        response = self.client.request("StopRecord")
        output = response.get("outputPath")
        deadline = time.monotonic() + 20
        while self.client.request("GetRecordStatus")["outputActive"]:
            if time.monotonic() > deadline:
                raise ObsError("OBS 녹화 종료 확인 시간이 초과되었습니다.")
            time.sleep(0.1)
        self.owned_record = False
        if not output:
            raise ObsError("OBS가 녹화 파일 경로를 반환하지 않았습니다. OBS 원래 녹화 폴더를 확인하세요.")
        self.last_path = Path(output)
        return self.last_path

    def restore(self) -> None:
        if not self.journal:
            return
        if self.client.request("GetRecordStatus")["outputActive"]:
            raise ObsError("OBS 녹화 중에는 원래 장면을 복원할 수 없습니다.")
        failures = []

        def call(request: str, data: dict) -> None:
            try:
                self.client.request(request, data)
            except ObsRequestError as error:
                if error.code != 600:  # ResourceNotFound: already removed by user/OBS.
                    failures.append(str(error))
            except Exception as error:
                failures.append(str(error))

        call("SetCurrentProgramScene", {"sceneName": self.journal["original_scene"]})
        for name, muted in self.journal["mutes"].items():
            call("SetInputMute", {"inputName": name, "inputMuted": muted})
        # Scene creation itself may have succeeded without its response reaching the client.
        call("RemoveScene", {"sceneName": self.journal["scene"]})
        for name in self.journal["inputs"]:
            call("RemoveInput", {"inputName": name})
        if failures:
            raise ObsError("OBS 일부 설정 복원에 실패했습니다. 다음 연결 때 다시 복원합니다: " + failures[0])
        self.journal_path.unlink(missing_ok=True)
        self.journal = None

    def close(self) -> None:
        try:
            if self.owned_record:
                try:
                    self.stop()
                except Exception:
                    self.client.connect()
                    if self.client.request("GetRecordStatus")["outputActive"]:
                        self.stop()
                    else:
                        self.owned_record = False
                        self.log("OBS는 종료됐지만 파일 경로를 받지 못했습니다. OBS 원래 녹화 폴더를 확인하세요.")
            self.restore()
        finally:
            self.client.close()
