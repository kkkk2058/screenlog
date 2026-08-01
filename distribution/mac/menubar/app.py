"""Screenlog 메뉴바 앱

녹화기(screenpipe)를 시작/중지하고 상태를 보여준다. 검색/질문은 여기서
안 한다 — 그건 웹(screenlog)이 담당한다. 이 앱은 "로컬 녹화기 스위치"로
역할을 좁힌다.

녹화기는 launchd가 아니라 이 앱이 자식 프로세스로 직접 띄운다. launchd로
백그라운드에서 조용히 띄우면, 첫 실행 때 뜨는 macOS 권한 팝업에 사용자가
응답하기도 전에 재시작을 반복하다가 launchd가 포기해버리는 문제가 있었다
(터미널에서 수동으로 한 번 실행해야만 풀렸음). 사용자가 실제로 더블클릭해서
연 GUI 앱이 직접 자식으로 띄우면, 그 시점에 정상적으로 권한 팝업이 뜬다.
"""

import datetime
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import rumps
from ApplicationServices import AXIsProcessTrustedWithOptions

DATA_DIR = Path.home() / ".screenpipe-redacted"
DB_PATH = DATA_DIR / "db.sqlite"
APP_LOG_PATH = DATA_DIR / "menubar.log"


def log(msg: str):
    """이 메뉴바 앱 자신의 동작 기록.

    Finder에서 더블클릭해서 띄운 GUI 앱은 표준출력이 /dev/null로 버려져서,
    print()를 아무리 해도 어디에도 안 남는다. 동기화 같은 백그라운드
    작업이 조용히 실패했을 때 사용자가 "메뉴 로그 보기"로 직접 원인을
    확인할 수 있게, 파일에 직접 쓴다.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(APP_LOG_PATH, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def ensure_accessibility_permission():
    """접근성 권한이 없으면 macOS 표준 요청 팝업을 띄운다.

    이 팝업은 "지금 열기" 버튼을 누르면 시스템 설정의 손쉬운 사용 화면을
    바로 열어주고, 이 앱을 목록에 미리 등록해준다 — 사용자가 직접
    시스템 설정을 찾아 들어가서 `+`로 추가할 필요가 없어진다. 권한
    자체를 코드로 켜는 건 불가능하다(보안상 사용자만 할 수 있음), 이건
    "그 화면으로 데려다주는 것"까지만 자동화한다.
    """
    options = {"AXTrustedCheckOptionPrompt": True}
    AXIsProcessTrustedWithOptions(options)

# 동기화(색인 + EC2 전송) 대상. 팀원 배포판에서는 이 값들을 설정 파일로
# 빼야 하지만, 지금은 개인 사용 기준으로 고정값을 쓴다.
SCREENLOG_DIR = Path.home() / "screenlog"
EC2_HOST = "15.164.242.189"
EC2_USER = "ubuntu"
EC2_KEY = Path.home() / "Downloads/EXPRESS-BEC.pem"
DASHBOARD_URL = "http://15.164.242.189:8000"
UV_BIN = Path.home() / ".local/bin/uv"


def resource_path(name: str) -> Path:
    """이 메뉴바 앱 안에 내장된 리소스(녹화기 실행파일)를 가리킨다.

    py2app으로 묶이면(.app) Contents/MacOS/Screenlog 옆에 Resources/가
    생기고, 그냥 `python app.py`로 직접 돌릴 때는 이 파일 옆의
    resources/ 폴더를 그대로 쓴다 — 개발 중에도 같은 코드로 테스트 가능.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent.parent / "Resources"
    else:
        base = Path(__file__).resolve().parent / "resources"
    return base / name


RECORDER_BIN = resource_path("screenpipe-bin")

RECORDER_ARGS = [
    str(RECORDER_BIN), "record",
    "--data-dir", str(DATA_DIR),
    "--async-pii-redaction",
    "--pii-redaction-labels", "secret,email,phone,person,address",
    "--pii-backend", "local",
]

RECORDER_ENV = {
    **os.environ,
    "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
            + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
}


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


def storage_used() -> str:
    if not DATA_DIR.exists():
        return "데이터 없음"
    total = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
    return human_size(total)


def redaction_progress() -> str:
    if not DB_PATH.exists():
        return "데이터 없음"
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN accessibility_redacted_at IS NOT NULL "
            "THEN 1 ELSE 0 END) FROM frames"
        ).fetchone()
        conn.close()
        total, done = row
        done = done or 0
        return f"{done}/{total} 처리됨"
    except sqlite3.Error:
        return "조회 실패 (녹화기 쓰는 중)"


class ScreenlogMenuBar(rumps.App):
    def __init__(self):
        super().__init__("Screenlog", icon=None, title="◌")
        self.process = None
        self.syncing = False

        self.status_item = rumps.MenuItem("상태 확인 중...")
        self.storage_item = rumps.MenuItem("저장 용량: -")
        self.redaction_item = rumps.MenuItem("리덕션: -")
        self.toggle_item = rumps.MenuItem("시작/중지", callback=self.toggle_recording)
        self.sync_item = rumps.MenuItem("지금 동기화 (색인 + 서버 전송)", callback=self.sync_now)
        self.dashboard_item = rumps.MenuItem("웹 대시보드 열기", callback=self.open_dashboard)
        self.log_item = rumps.MenuItem("동기화 로그 보기", callback=self.open_log)

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            None,
            self.storage_item,
            self.redaction_item,
            None,
            self.sync_item,
            self.log_item,
            self.dashboard_item,
        ]

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ensure_accessibility_permission()
        self.start_recording()

        self.timer = rumps.Timer(self.refresh, 5)
        self.timer.start()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def refresh(self, _):
        running = self.is_running()
        if not self.syncing:
            self.title = "●" if running else "○"
        self.status_item.title = "● 녹화 중" if running else "○ 꺼짐"
        self.toggle_item.title = "중지" if running else "시작"
        self.storage_item.title = f"저장 용량: {storage_used()}"
        self.redaction_item.title = f"리덕션: {redaction_progress()}"

    def start_recording(self):
        if self.is_running():
            return
        log = open(DATA_DIR / "recorder.log", "a")
        self.process = subprocess.Popen(
            RECORDER_ARGS, env=RECORDER_ENV, stdout=log, stderr=log,
        )
        self.refresh(None)

    def stop_recording(self):
        if not self.is_running():
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.refresh(None)

    def toggle_recording(self, _):
        if self.is_running():
            self.stop_recording()
        else:
            self.start_recording()

    def sync_now(self, _):
        self.sync_item.title = "동기화 중... (색인)"
        self.syncing = True
        self.title = "⟳"
        log("동기화 시작 (버튼 클릭됨)")
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        try:
            if not SCREENLOG_DIR.exists():
                log(f"실패: SCREENLOG_DIR 없음 ({SCREENLOG_DIR})")
                rumps.notification("Screenlog", "동기화 실패", f"{SCREENLOG_DIR} 없음")
                return
            if not UV_BIN.exists():
                log(f"실패: uv 실행파일 없음 ({UV_BIN})")
                rumps.notification("Screenlog", "동기화 실패", f"uv 없음: {UV_BIN}")
                return

            rumps.notification("Screenlog", "동기화 시작", "색인 중... (몇 분 걸릴 수 있음)")

            index_cmd = [str(UV_BIN), "run", "python", "-m", "screenlog.index"]
            log(f"색인 시작: {' '.join(index_cmd)} (cwd={SCREENLOG_DIR})")
            try:
                result = subprocess.run(
                    index_cmd, cwd=str(SCREENLOG_DIR), env=RECORDER_ENV,
                    capture_output=True, text=True,
                )
            except FileNotFoundError as e:
                log(f"색인 실패(명령어 못 찾음): {e}")
                rumps.notification("Screenlog", "색인 실패", f"명령어를 못 찾음: {e}")
                return
            if result.returncode != 0:
                log(f"색인 실패(returncode={result.returncode}): {result.stderr[-1000:]}")
                rumps.notification("Screenlog", "색인 실패", result.stderr[-200:] or "알 수 없는 오류")
                return
            log(f"색인 완료: {result.stdout[-500:]}")

            self.sync_item.title = "동기화 중... (서버 전송)"
            rsync_cmd = [
                "/usr/bin/rsync", "-az", "-e", f"ssh -i {EC2_KEY}",
                f"{SCREENLOG_DIR}/chroma/", f"{EC2_USER}@{EC2_HOST}:~/screenlog/chroma/",
            ]
            log(f"서버 전송 시작: {' '.join(rsync_cmd)}")
            result = subprocess.run(rsync_cmd, env=RECORDER_ENV, capture_output=True, text=True)
            if result.returncode != 0:
                log(f"서버 전송 실패(returncode={result.returncode}): {result.stderr[-1000:]}")
                rumps.notification("Screenlog", "서버 전송 실패", result.stderr[-200:] or "알 수 없는 오류")
                return

            log("동기화 완료")
            rumps.notification("Screenlog", "동기화 완료", "서버에 최신 데이터가 반영됐습니다.")
        finally:
            self.sync_item.title = "지금 동기화 (색인 + 서버 전송)"
            self.syncing = False
            self.refresh(None)

    def open_dashboard(self, _):
        os.system(f"open {DASHBOARD_URL}")

    def open_log(self, _):
        APP_LOG_PATH.touch(exist_ok=True)
        os.system(f"open -a TextEdit {APP_LOG_PATH}")


if __name__ == "__main__":
    ScreenlogMenuBar().run()
