from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recorder.browser import CourseBrowser
from recorder.config import Config
from recorder.obs import authentication

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def course_site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path in ("/one.mp4", "/two.mp4", "/one.webm", "/two.webm"):
                payload = (FIXTURES / parsed.path.lstrip("/")).read_bytes()
                content_type = "video/webm" if parsed.path.endswith("webm") else "video/mp4"
            elif parsed.path == "/outer.html":
                unit = query.get("unit", ["unit4553"])[0]
                payload = f'<iframe allow="autoplay; fullscreen" width="400" height="260" src="/player.html?unit={unit}"></iframe>'.encode()
                content_type = "text/html; charset=utf-8"
            elif parsed.path == "/player.html":
                unit = query.get("unit", ["unit4553"])[0]
                media = "one.webm" if unit == "unit4553" else "two.webm"
                payload = f'''<!doctype html><html><body>
                  <video id="main-video" width="320" height="180" preload="none" src="/{media}" playsinline></video>
                  <button class="plyr__control--overlaid" data-plyr="play" onclick="document.querySelector('video').play()">재생</button>
                </body></html>'''.encode()
                content_type = "text/html; charset=utf-8"
            else:
                broken = "true" if "broken" in query else "false"
                payload = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
                <style>li.unit_line {{display:none}} li.unit_line.show {{display:block}}
                .course_timeline {{width:290px; height:180px; overflow:auto}} li {{padding:15px}}
                .course_content_panel {{margin-top:20px}}</style></head><body>
                <div class="course_timeline accordion"><ul>
                  <li class="section show"><h4>딥러닝이론</h4></li>
                  <li id="unit4553" class="unit_line done show active"><a>첫 번째 강의</a></li>
                  <li class="section"><h4>Project 1</h4></li>
                  <li id="unit4182" class="unit_line done"><a>두 번째 강의</a></li>
                  <li id="unit7777" class="unit_line"><a>영상 없는 읽기 자료</a></li>
                  <li id="unit4245" class="unit_line"><a>세 번째 강의</a></li>
                  <li id="unit9999" class="unit_line"><a>플레이어가 바뀌지 않는 강의</a></li>
                </ul></div>
                <div class="course_content_panel"><h3 id="title">첫 번째 강의</h3>
                  <iframe id="player" allow="autoplay; fullscreen" width="450" height="310"
                    src="http://localhost:{player_server.server_port}/outer.html?unit=unit4553"></iframe>
                  <article id="material" hidden><h2>읽기 자료</h2><p>영상이 없는 텍스트 자료입니다.</p></article></div>
                <script>
                const broken={broken};
                document.querySelectorAll('li.section').forEach(s=>s.onclick=()=>{{
                  const shown=!s.classList.contains('show');s.classList.toggle('show',shown);
                  let node=s.nextElementSibling; while(node && !node.classList.contains('section')){{
                    node.classList.toggle('show',shown); node=node.nextElementSibling;
                  }}
                }});
                document.querySelectorAll('li.unit_line').forEach(u=>u.onclick=()=>{{
                  document.querySelectorAll('li.unit_line').forEach(x=>x.classList.remove('active'));
                  u.classList.add('active');document.querySelector('#title').textContent=u.querySelector('a').textContent;
                  const player=document.querySelector('#player'), material=document.querySelector('#material');
                  if(u.id==='unit7777'){{ player.style.display='none'; material.hidden=false; return; }}
                  material.hidden=true; player.style.display='';
                  if(u.id==='unit9999' || broken) return;
                  setTimeout(()=>{{player.src=
                    'http://localhost:{player_server.server_port}/outer.html?unit='+u.id;}}, 700);
                }});
                </script></body></html>'''.encode()
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    player_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    main_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (player_server, main_server)]
    for thread in threads:
        thread.start()
    yield f"http://127.0.0.1:{main_server.server_port}/course"
    for server in (main_server, player_server):
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=3)


@pytest.fixture
def cfg(course_site, tmp_path):
    return Config(course_url=course_site, browser="chromium", auto_scene=False,
                  output_dir=str(tmp_path / "recordings"), min_free_mb=0, retries=0,
                  load_timeout=4, material_timeout=0.8, stall_timeout=3,
                  remux_mp4=False, fullscreen_video=False)


@pytest.fixture
def browser_factory():
    from playwright.sync_api import sync_playwright

    executable = os.environ.get("LECTURE_TEST_BROWSER")
    if executable and not Path(executable).is_file():
        pytest.fail("LECTURE_TEST_BROWSER does not exist")
    if not executable:
        with sync_playwright() as p:
            default = Path(p.chromium.executable_path)
            if not default.exists():
                pytest.skip("Install Playwright Chromium or set LECTURE_TEST_BROWSER for browser integration tests")

    def factory(config, base, log):
        return CourseBrowser(config, base, log, headless=True, executable_path=executable)
    return factory


@pytest.fixture
def browser(cfg, tmp_path, browser_factory):
    result = browser_factory(cfg, tmp_path, lambda _: None)
    result.open()
    # Ensure the initially visible iframe has loaded before collecting a baseline.
    result.page.wait_for_timeout(500)
    yield result
    result.close()


class FakeObs:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir()
        self.password, self.salt, self.challenge = "test-only", "sample-salt", "sample-challenge"
        self.active, self.paused = False, False
        self.requests = []
        self.inputs = {"Desktop Audio": {"muted": False}, "Mic/Aux": {"muted": False}}
        self.scenes, self.current = {"Original"}, "Original"
        self.sequence, self.fail_once = 0, None
        self.server = serve(self.handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def handle(self, connection):
        try:
            connection.send(json.dumps({"op": 0, "d": {"rpcVersion": 1, "authentication": {"salt": self.salt, "challenge": self.challenge}}}))
            identify = json.loads(connection.recv())
            if identify.get("op") != 1 or identify["d"].get("authentication") != authentication(self.password, self.salt, self.challenge):
                connection.close(code=1008, reason="bad auth")
                return
            connection.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
            for message in connection:
                request = json.loads(message)["d"]
                name, data = request["requestType"], request.get("requestData", {})
                self.requests.append((name, data))
                code, result = 100, {}
                if self.fail_once == name:
                    self.fail_once, code = None, 500
                elif name == "GetVersion":
                    result = {"obsVersion": "mock-31.0", "obsWebSocketVersion": "5.5.0"}
                elif name == "GetRecordStatus":
                    result = {"outputActive": self.active, "outputPaused": self.paused, "outputDuration": 0}
                elif name == "GetStreamStatus":
                    result = {"outputActive": False}
                elif name == "GetRecordDirectory":
                    result = {"recordDirectory": str(self.directory)}
                elif name == "StartRecord":
                    if self.active:
                        code = 500
                    else:
                        self.active = True
                        self.sequence += 1
                elif name == "StopRecord":
                    if not self.active:
                        code = 501
                    else:
                        self.active = False
                        output = self.directory / f"record_{self.sequence:03d}.mp4"
                        shutil.copy2(FIXTURES / "two.mp4", output)
                        result = {"outputPath": str(output)}
                elif name == "GetInputKindList":
                    result = {"inputKinds": ["monitor_capture", "wasapi_output_capture"]}
                elif name == "GetCurrentProgramScene":
                    result = {"currentProgramSceneName": self.current}
                elif name == "GetSpecialInputs":
                    result = {"desktop1": "Desktop Audio", "mic1": "Mic/Aux", "desktop2": None}
                elif name == "GetInputMute":
                    result = {"inputMuted": self.inputs[data["inputName"]]["muted"]}
                elif name == "SetInputMute":
                    if data["inputName"] not in self.inputs:
                        code = 600
                    else:
                        self.inputs[data["inputName"]]["muted"] = data["inputMuted"]
                elif name == "GetInputDefaultSettings":
                    result = {"defaultInputSettings": {}}
                elif name == "GetVideoSettings":
                    result = {"baseWidth": 1920, "baseHeight": 1080}
                elif name == "GetSceneItemId":
                    result = {"sceneItemId": 1}
                elif name == "CreateScene":
                    self.scenes.add(data["sceneName"])
                elif name == "RemoveScene":
                    if data["sceneName"] not in self.scenes:
                        code = 600
                    else:
                        self.scenes.remove(data["sceneName"])
                elif name == "SetCurrentProgramScene":
                    if data["sceneName"] not in self.scenes:
                        code = 600
                    else:
                        self.current = data["sceneName"]
                elif name == "CreateInput":
                    self.inputs[data["inputName"]] = {"muted": False}
                    result = {"sceneItemId": 1}
                elif name == "RemoveInput":
                    if self.inputs.pop(data["inputName"], None) is None:
                        code = 600
                elif name in ("SetInputVolume", "SetInputAudioTracks", "SetSceneItemTransform"):
                    pass
                else:
                    code = 204
                # An irrelevant event must not be mistaken for the matching request response.
                connection.send(json.dumps({"op": 5, "d": {"eventType": "TestEvent"}}))
                connection.send(json.dumps({"op": 7, "d": {"requestId": request["requestId"], "requestType": name,
                    "requestStatus": {"result": code == 100, "code": code, "comment": "mock"}, "responseData": result}}))
        except ConnectionClosed:
            pass

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=3)


@pytest.fixture
def fake_obs(tmp_path):
    fake = FakeObs(tmp_path / "obs")
    yield fake
    fake.stop()
