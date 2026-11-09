from __future__ import annotations

import os
import queue
import subprocess
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .common import RecorderError, clock
from .config import Config
from .engine import Worker


class App:
    def __init__(self, root: tk.Tk, base: Path, config_path: Path):
        self.root, self.base, self.config_path = root, base, config_path
        self.cfg = Config.load(config_path)
        self.worker = Worker(base)
        self.busy = self.closing = False
        self.checked: set[str] = set()
        self.lessons: list[dict] = []
        self.vars = {}
        self.controls = []
        root.title("강의 자동 녹화 · Lecture Recorder")
        root.geometry("1160x900")
        root.minsize(930, 720)
        root.configure(bg="#f5f7fb")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        root.after(100, self.poll)

    def _build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f7fb")
        style.configure("TLabel", background="#f5f7fb", font=("맑은 고딕", 10))
        style.configure("TLabelframe", background="#f5f7fb", bordercolor="#d8dfec")
        style.configure("TLabelframe.Label", background="#f5f7fb", foreground="#253957", font=("맑은 고딕", 11, "bold"))
        style.configure("TButton", font=("맑은 고딕", 10), padding=(12, 7))
        style.configure("Accent.TButton", background="#245be6", foreground="white")
        style.map("Accent.TButton", background=[("active", "#1949c1"), ("disabled", "#c7d3ec")])
        style.configure("Stop.TButton", background="#e04b55", foreground="white")
        style.configure("TCheckbutton", background="#f5f7fb", font=("맑은 고딕", 10))
        style.configure("Treeview", rowheight=29, font=("맑은 고딕", 10), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"), padding=7)
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="강의 자동 녹화", font=("맑은 고딕", 23, "bold"), foreground="#162846").pack(anchor="w")
        ttk.Label(outer, text="목차 순서대로 재생 · 강의별 파일 저장 · 완료 파일을 확인하고 이어서 녹화", foreground="#68748a").pack(anchor="w", pady=(3, 12))

        settings = ttk.LabelFrame(outer, text="연결 설정", padding=12)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        self._entry(settings, "강의 주소", "course_url", row=0)
        self.vars["browser"] = tk.StringVar(value={"chrome": "Chrome", "msedge": "Edge", "chromium": "Chromium"}[self.cfg.browser])
        ttk.Label(settings, text="브라우저").grid(row=0, column=2, padx=(14, 6))
        browser = ttk.Combobox(settings, textvariable=self.vars["browser"], values=["Chrome", "Edge", "Chromium"], width=10, state="readonly")
        browser.grid(row=0, column=3, sticky="e")
        self.controls.append((browser, "readonly"))
        self._entry(settings, "저장 폴더", "output_dir", row=1)
        browse = ttk.Button(settings, text="폴더 선택", command=self.browse)
        browse.grid(row=1, column=2, columnspan=2, sticky="e", pady=4)
        self.controls.append((browse, "normal"))

        obs = ttk.Frame(settings)
        obs.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        for name, label, width in [("obs_host", "OBS 호스트", 15), ("obs_port", "포트", 6), ("obs_password", "비밀번호", 20)]:
            ttk.Label(obs, text=label).pack(side="left", padx=(0 if name == "obs_host" else 12, 5))
            self.vars[name] = tk.StringVar(value=str(getattr(self.cfg, name)))
            entry = ttk.Entry(obs, textvariable=self.vars[name], width=width, show="●" if name == "obs_password" else "")
            entry.pack(side="left")
            self.controls.append((entry, "normal"))
        self._check(obs, "remember_password", "비밀번호 기억", pack=True)
        test = ttk.Button(obs, text="OBS 연결 테스트", command=lambda: self.submit("test_obs"))
        test.pack(side="right")
        self.controls.append((test, "normal"))

        options = ttk.Frame(outer)
        options.pack(fill="x", pady=9)
        for column, (name, label) in enumerate([
            ("auto_scene", "OBS 화면·PC 소리 자동 구성"),
            ("resume", "완료 파일 확인 후 이어서 녹화"),
            ("remux_mp4", "MP4 변환 (FFmpeg 필요)"),
        ]):
            self._check(options, name, label, row=0, column=column)
        for column, (name, label) in enumerate([
            ("fullscreen_video", "영상 전체 화면"),
            ("playback_only", "녹화 없이 영상 이동 테스트"),
            ("continue_on_error", "실패한 강의는 다음으로 이동"),
        ]):
            self._check(options, name, label, row=1, column=column)
        ttk.Label(outer, text="자동 구성은 기본 모니터와 기본 PC 소리를 캡처합니다. 녹화가 시작되면 이 창은 최소화됩니다.", foreground="#68748a").pack(anchor="w", pady=(0, 7))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 10))
        for command, label, accent in [("open", "1. 브라우저 열기", False), ("catalog", "2. 강의 목록 읽기", False), ("record", "3. 선택 강의 녹화", True)]:
            button = ttk.Button(actions, text=label, command=lambda cmd=command: self.submit(cmd), style="Accent.TButton" if accent else "TButton")
            button.pack(side="left", padx=(0, 8))
            self.controls.append((button, "normal"))
        self.stop_button = ttk.Button(actions, text="중지", style="Stop.TButton", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(actions, text="저장 폴더 열기", command=self.open_output).pack(side="right")

        list_actions = ttk.Frame(outer)
        list_actions.pack(fill="x")
        self.selection_label = tk.StringVar(value="브라우저에서 로그인한 후 '강의 목록 읽기'를 누르세요.")
        ttk.Label(list_actions, textvariable=self.selection_label, foreground="#253957").pack(side="left")
        for label, selected in [("전체 선택", True), ("선택 해제", False)]:
            button = ttk.Button(list_actions, text=label, command=lambda value=selected: self.select_all(value))
            button.pack(side="right", padx=3)
            self.controls.append((button, "normal"))

        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True, pady=(6, 10))
        self.tree = ttk.Treeview(table, columns=("check", "done", "status"), height=10, selectmode="browse")
        self.tree.heading("#0", text="대목차 / 강의 제목")
        self.tree.heading("check", text="선택")
        self.tree.heading("done", text="사이트 수강")
        self.tree.heading("status", text="녹화 상태")
        self.tree.column("#0", width=615, minwidth=290)
        for name, width in [("check", 58), ("done", 90), ("status", 140)]:
            self.tree.column(name, width=width, minwidth=width, stretch=False, anchor="center")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("section", background="#edf2fb", foreground="#263f68", font=("맑은 고딕", 10, "bold"))
        self.tree.bind("<ButtonRelease-1>", self.toggle_row)
        self.tree.bind("<space>", self.toggle_row)

        self.status = tk.StringVar(value="대기 중 · 처음에는 짧은 강의 하나를 선택해 녹화 결과를 확인하세요.")
        ttk.Label(outer, textvariable=self.status, font=("맑은 고딕", 11, "bold"), foreground="#253957").pack(anchor="w")
        self.progress = ttk.Progressbar(outer, maximum=100)
        self.progress.pack(fill="x", pady=(7, 3))
        self.counts = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.counts, foreground="#68748a").pack(anchor="w")
        self.logs = tk.Text(outer, height=6, wrap="word", font=("맑은 고딕", 9), bg="#152139", fg="#dce5f6",
                            relief="flat", padx=10, pady=8, state="disabled")
        self.logs.pack(fill="x", pady=(9, 0))

    def _entry(self, parent, label, name, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        self.vars[name] = tk.StringVar(value=str(getattr(self.cfg, name)))
        entry = ttk.Entry(parent, textvariable=self.vars[name])
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        self.controls.append((entry, "normal"))

    def _check(self, parent, name, label, row=0, column=0, pack=False):
        self.vars[name] = tk.BooleanVar(value=getattr(self.cfg, name))
        widget = ttk.Checkbutton(parent, text=label, variable=self.vars[name])
        if pack:
            widget.pack(side="left", padx=10)
        else:
            widget.grid(row=row, column=column, sticky="w", padx=(0, 16), pady=3)
        self.controls.append((widget, "normal"))

    def collect(self) -> Config:
        changes = {name: variable.get() for name, variable in self.vars.items()}
        changes["browser"] = {"Chrome": "chrome", "Edge": "msedge", "Chromium": "chromium"}[changes["browser"]]
        try:
            changes["obs_port"] = int(changes["obs_port"])
        except ValueError as error:
            raise RecorderError("OBS 포트에는 숫자를 입력하세요.") from error
        changes["course_url"] = changes["course_url"].strip()
        return replace(self.cfg, **changes)

    def set_busy(self, busy: bool):
        self.busy = busy
        for widget, normal in self.controls:
            widget.configure(state="disabled" if busy else normal)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def submit(self, command: str):
        if self.busy or self.closing:
            return
        try:
            cfg = self.collect()
            cfg.validate(need_url=command != "test_obs")
            if command == "record" and not self.checked:
                raise RecorderError("녹화할 강의를 선택하세요.")
            cfg.save(self.config_path)
            self.cfg = cfg
            self.set_busy(True)
            self.status.set({"open": "브라우저를 여는 중…", "catalog": "강의 목록을 읽는 중…",
                             "test_obs": "OBS에 연결하는 중…", "record": "녹화를 준비하는 중…"}[command])
            self.worker.submit(command, cfg, list(self.checked))
        except Exception as error:
            messagebox.showerror("설정 확인", str(error), parent=self.root)

    def browse(self):
        folder = filedialog.askdirectory(initialdir=str(self.cfg.output_path(self.base)), parent=self.root)
        if folder:
            self.vars["output_dir"].set(folder)

    def select_all(self, selected: bool):
        if self.busy:
            return
        self.checked = {lesson["unit_id"] for lesson in self.lessons} if selected else set()
        self.refresh_checks()

    def toggle_row(self, event):
        if self.busy:
            return "break"
        row = self.tree.focus() if event.keysym == "space" else self.tree.identify_row(event.y)
        if not row:
            return
        ids = list(self.tree.get_children(row)) if row.startswith("section_") else [row]
        if all(identifier in self.checked for identifier in ids):
            self.checked.difference_update(ids)
        else:
            self.checked.update(ids)
        self.refresh_checks()
        return "break" if event.keysym == "space" else None

    def refresh_checks(self):
        for lesson in self.lessons:
            identifier = lesson["unit_id"]
            self.tree.set(identifier, "check", "✓" if identifier in self.checked else "")
        for row in self.tree.get_children():
            children = self.tree.get_children(row)
            self.tree.set(row, "check", "✓" if children and all(x in self.checked for x in children) else "")
        self.selection_label.set(f"총 {len(self.lessons)}개 강의 · 선택 {len(self.checked)}개 · 행을 클릭하거나 Space로 선택")

    def show_catalog(self, event):
        self.tree.delete(*self.tree.get_children())
        self.lessons = event["lessons"]
        completed = set(event["completed"])
        materials = set(event.get("materials", []))
        self.checked = {lesson["unit_id"] for lesson in self.lessons}
        for lesson in self.lessons:
            parent = f"section_{lesson['section_index']}"
            if not self.tree.exists(parent):
                self.tree.insert("", "end", iid=parent, text=lesson["section_title"], open=True, tags=("section",))
            self.tree.insert(parent, "end", iid=lesson["unit_id"],
                             text=f"{lesson['unit_index'] + 1:02d}. {lesson['title']}",
                             values=("✓", "완료" if lesson["site_done"] else "미수강",
                                     "자료 · 건너뜀" if lesson["unit_id"] in materials else
                                     ("완료 파일 있음" if lesson["unit_id"] in completed else "대기")))
        self.refresh_checks()

    def add_log(self, text):
        self.logs.configure(state="normal")
        self.logs.insert("end", text + "\n")
        if int(self.logs.index("end-1c").split(".")[0]) > 600:
            self.logs.delete("1.0", "100.0")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def poll(self):
        try:
            while True:
                event = self.worker.events.get_nowait()
                kind = event["type"]
                if kind == "log":
                    self.add_log(event["message"])
                elif kind == "catalog":
                    self.show_catalog(event)
                    self.status.set("강의 목록을 읽었습니다. 선택 후 녹화를 시작하세요.")
                elif kind == "obs_ok":
                    self.status.set("OBS 연결 정상")
                elif kind == "recording_started":
                    if self.cfg.minimize_on_record and not self.cfg.playback_only:
                        self.root.iconify()
                elif kind == "lesson":
                    if self.tree.exists(event["unit_id"]):
                        self.tree.set(event["unit_id"], "status", event["status"])
                        self.tree.see(event["unit_id"])
                    if event.get("title"):
                        self.status.set(event["section"] + " / " + event["title"])
                        self.progress["value"] = 0
                elif kind == "progress":
                    self.progress["value"] = event["percent"]
                    self.counts.set(f"{clock(event['current'])} / {clock(event['duration'])} · {event['percent']:.1f}%")
                elif kind == "counts":
                    self.counts.set(f"완료 {event['completed']} · 기존 파일 {event['skipped']} · 자료 {event.get('materials', 0)} · 실패 {event['failed']} / 선택 {event['total']}")
                elif kind == "summary":
                    ending = "중지됨" if event["cancelled"] else ("오류로 중단됨" if event["aborted"] else "처리 완료")
                    label = "재생 테스트" if event["playback_only"] else "녹화"
                    self.status.set(f"{label} {ending} · 완료 {event['completed']} / 기존 파일 {event['skipped']} / 자료 {event.get('materials', 0)} / 실패 {event['failed']}")
                    self.root.deiconify()
                elif kind == "error":
                    self.status.set(event["message"])
                    self.add_log("확인 필요: " + event["message"])
                elif kind == "done":
                    if not self.closing:
                        self.set_busy(False)
                elif kind == "closed":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def stop(self):
        self.worker.stop()
        self.status.set("중지 요청됨 · 현재 녹화 파일을 보관하고 OBS를 정리하는 중…")

    def open_output(self):
        folder = self.collect().output_path(self.base)
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)
        else:
            subprocess.Popen(["open" if __import__("sys").platform == "darwin" else "xdg-open", str(folder)])

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.set_busy(True)
        self.worker.stop()
        self.status.set("종료 중 · 녹화 파일과 브라우저를 정리하고 있습니다…")
        self.worker.submit("close")


def launch(base: Path, config_path: Path):
    root = tk.Tk()
    try:
        App(root, base, config_path)
        root.mainloop()
    except Exception:
        root.destroy()
        raise
