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
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
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
    with open(APP_LOG_PATH, "a", encoding="utf-8") as f:
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

# 동기화 설정. 예전엔 여기에 EC2 주소·SSH 개인키 경로·~/screenlog 저장소
# 경로·uv 실행파일 경로가 상수로 박혀 있었다. 그건 개발자 본인 맥에서만
# 성립하는 값들이라, dmg만 받은 사람은 "SCREENLOG_DIR 없음"으로 바로
# 실패했다. 이제 앱은 자기 안에 screenlog 패키지를 들고 있고, 서버로는
# HTTP로 올린다(SSH 개인키를 배포할 방법이 없다).
DEFAULT_SERVER_URL = "http://3.35.7.225:8000"
CONFIG_PATH = DATA_DIR / "config.json"


def load_config() -> dict:
    """서버 주소와 계정. 사용자가 메뉴에서 입력한 값이 여기 남는다."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    # 비밀번호가 들어 있으니 남이 읽지 못하게 한다.
    CONFIG_PATH.chmod(0o600)


def server_url() -> str:
    return load_config().get("server_url") or DEFAULT_SERVER_URL


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
    # DATA_DIR 이름(.screenpipe-redacted)만 믿고 이 플래그들이 빠져 있었다 —
    # 실제로는 리덕션 없이 원본 텍스트가 그대로 기록되고 있었음.
    "--async-pii-redaction",
    "--pii-redaction-labels", "secret,email,phone,person,address",
    "--pii-backend", "local",
    # 기본값(whisper-tiny)이 한국어를 다른 언어로 잘못 알아듣는 경우가 잦아서
    # (실측: 러시아어/일본어/중국어로 오인식) 더 큰 모델로 바꿨다. 대신 훨씬
    # 무겁다 — CPU/배터리 사용량과 첫 실행 시 모델 다운로드 용량이 커진다.
    "--audio-transcription-engine", "whisper-large",
]


def _kill_stale_recorder():
    """이전 실행이 남긴 recorder가 있으면 정리한다.

    이 앱은 Quit해도 자식 프로세스(recorder)를 안 죽이고 그냥 앱만 종료됐다
    (Quit 핸들러 자체가 없었다) — 그러면 recorder가 고아 프로세스로 계속
    돌아간다. 다음에 앱을 다시 열면 __init__이 무조건 새 recorder를 띄우는데,
    포트가 고아 프로세스한테 이미 점유돼 있어서 새 프로세스는 시작하자마자
    죽는다. 그 결과 실제로는 옛 recorder가 멀쩡히 녹화 중인데도 메뉴바
    UI(is_running())는 "꺼짐"으로 잘못 표시한다 — 사용자 눈엔 "자꾸 꺼진다"로
    보이는 원인이 이거였다. 시작 전에 같은 data-dir로 뜬 recorder를 찾아서
    정리하면, 어떤 식으로 죽었든(강제종료/크래시 포함) 항상 깨끗하게 하나만
    남는다.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"screenpipe-bin record --data-dir {DATA_DIR}"],
            capture_output=True, text=True,
        )
    except Exception:
        return
    pids = [int(p) for p in result.stdout.split() if p.strip()]
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(1)   # 포트가 반납될 시간을 준다
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


RECORDER_ENV = {
    **os.environ,
    "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
            + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
    # Finder에서 띄운 GUI 앱은 터미널과 달리 로케일이 안 넘어와서 기본값이
    # ASCII가 된다. 한글 섞인 출력(색인 로그 등)을 다루는 자식 프로세스가
    # 이걸로 죽는 걸 막는다.
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
}

# (예전엔 여기 SYNC_ENV가 있었다 — `uv run python -m screenlog.index`로
# 띄우던 별도 색인 프로세스가 이 앱의 PYTHONHOME/PYTHONPATH를 물려받아
# 죽는 걸 막는 용도였다. 이제 색인을 같은 프로세스 안에서 직접 부르므로
# 물려줄 환경 자체가 없어져서 지웠다.)


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
        # quit_button=None으로 rumps 기본 Quit 항목을 끄고, 우리 정리 로직을
        # 거치는 Quit 항목을 직접 넣는다(아래 menu 리스트) — 기본 Quit은
        # recorder 자식 프로세스를 안 죽이고 앱만 종료해서 고아 프로세스가
        # 남았다.
        super().__init__("Screenlog", icon=None, title="◌", quit_button=None)
        self.process = None
        self.syncing = False

        self.status_item = rumps.MenuItem("상태 확인 중...")
        self.storage_item = rumps.MenuItem("저장 용량: -")
        self.redaction_item = rumps.MenuItem("리덕션: -")
        self.toggle_item = rumps.MenuItem("시작/중지", callback=self.toggle_recording)
        self.sync_item = rumps.MenuItem("지금 동기화 (색인 + 서버 전송)", callback=self.sync_now)
        self.settings_item = rumps.MenuItem("서버 설정", callback=self.edit_settings)
        self.dashboard_item = rumps.MenuItem("웹 대시보드 열기", callback=self.open_dashboard)
        self.log_item = rumps.MenuItem("동기화 로그 보기", callback=self.open_log)
        self.quit_item = rumps.MenuItem("Quit", callback=self.quit_app)

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            None,
            self.storage_item,
            self.redaction_item,
            None,
            self.sync_item,
            self.settings_item,
            self.log_item,
            self.dashboard_item,
            None,
            self.quit_item,
        ]

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ensure_accessibility_permission()
        _kill_stale_recorder()
        self.start_recording()

        self.timer = rumps.Timer(self.refresh, 5)
        self.timer.start()

    def quit_app(self, _):
        self.stop_recording()
        rumps.quit_application()

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
        try:
            self.sync_item.title = "동기화 중... (색인)"
            self.syncing = True
            self.title = "⟳"
            log("동기화 시작 (버튼 클릭됨)")
            threading.Thread(target=self._sync_worker, daemon=True).start()
        except Exception:
            import traceback
            tb = traceback.format_exc()
            try:
                with open(DATA_DIR / "menubar-crash.log", "a", encoding="utf-8") as f:
                    f.write(tb + "\n")
            except Exception:
                pass
            rumps.alert("동기화 시작 실패", tb[-800:])
            self.sync_item.title = "지금 동기화 (색인 + 서버 전송)"
            self.syncing = False
            self.refresh(None)

    def _sync_worker(self):
        try:
            config = load_config()
            if not (config.get("user") and config.get("password")):
                log("실패: 서버 계정 미설정")
                rumps.notification("Screenlog", "서버 설정 필요",
                                    "메뉴의 '서버 설정'에서 아이디/비밀번호를 입력하세요.")
                return

            # screenlog는 설정을 import 시점에 환경변수로 읽는다. 그래서
            # 반드시 import보다 먼저 채워야 한다 — 순서가 바뀌면 사용자가
            # 방금 입력한 계정 대신 빈 값이 박힌 채로 굳는다.
            os.environ["SCREENLOG_ROLE"] = "client"
            os.environ["SCREENLOG_SERVER_URL"] = config.get("server_url") or DEFAULT_SERVER_URL
            os.environ["SCREENLOG_USER"] = config["user"]
            os.environ["SCREENLOG_PASSWORD"] = config["password"]
            os.environ["SCREENLOG_DATA_DIR"] = str(DATA_DIR / "screenlog")

            from screenlog.index import model_is_ready
            from screenlog.sync import SyncError, sync_all

            if not model_is_ready():
                rumps.notification("Screenlog", "첫 실행 준비",
                                    "검색용 모델(약 2GB)을 내려받습니다. 한 번만 받으면 됩니다.")

            def on_model_progress(done, total):
                percent = (done / total * 100) if total else 0
                self.sync_item.title = f"모델 내려받는 중... {percent:.0f}% ({human_size(done)})"

            def on_status(message):
                self.sync_item.title = f"동기화 중... {message}"
                log(message)

            log(f"동기화 시작 (서버={os.environ['SCREENLOG_SERVER_URL']})")
            rumps.notification("Screenlog", "동기화 시작", "기록을 정리해 서버로 올립니다.")

            try:
                uploaded, days = sync_all(on_status=on_status,
                                           on_model_progress=on_model_progress)
            except SyncError as e:
                # 사용자가 고칠 수 있는 실패(계정 오류, 서버 꺼짐 등)는
                # 문구를 그대로 보여준다.
                log(f"동기화 실패: {e}")
                rumps.notification("Screenlog", "동기화 실패", str(e)[:200])
                return

            log(f"동기화 완료: {days}일치에서 {uploaded}건 업로드")
            if uploaded:
                rumps.notification("Screenlog", "동기화 완료",
                                    f"{uploaded}건을 서버에 올렸습니다.")
            else:
                rumps.notification("Screenlog", "동기화 완료", "새로 올릴 기록이 없습니다.")
        except Exception:
            import traceback
            tb = traceback.format_exc()
            log(f"동기화 중 예상치 못한 오류:\n{tb}")
            rumps.notification("Screenlog", "동기화 실패", tb.strip().splitlines()[-1][:200])
        finally:
            self.sync_item.title = "지금 동기화 (색인 + 서버 전송)"
            self.syncing = False
            self.refresh(None)

    def edit_settings(self, _):
        """서버 주소와 계정을 입력받는다.

        예전엔 EC2 주소와 SSH 개인키 경로가 코드에 상수로 박혀 있어서
        개발자 본인 맥에서만 동기화가 됐다. 배포되는 앱은 받는 사람마다
        계정이 다르므로 값을 물어봐야 한다."""
        config = load_config()
        for key, label, default in (
            ("server_url", "서버 주소", config.get("server_url") or DEFAULT_SERVER_URL),
            ("user", "아이디", config.get("user", "")),
            ("password", "비밀번호", config.get("password", "")),
        ):
            response = rumps.Window(
                message=f"{label}을(를) 입력하세요.", title="Screenlog 서버 설정",
                default_text=default, ok="확인", cancel="취소", dimensions=(300, 24),
            ).run()
            if not response.clicked:
                return          # 취소하면 여태 입력한 것도 저장하지 않는다
            config[key] = response.text.strip()

        save_config(config)
        log(f"서버 설정 저장됨 (서버={config.get('server_url')}, 아이디={config.get('user')})")
        rumps.notification("Screenlog", "설정 저장됨", config.get("server_url", ""))

    def open_dashboard(self, _):
        os.system(f"open {server_url()}")

    def open_log(self, _):
        APP_LOG_PATH.touch(exist_ok=True)
        os.system(f"open -a TextEdit {APP_LOG_PATH}")


if __name__ == "__main__":
    ScreenlogMenuBar().run()
