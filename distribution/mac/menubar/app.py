"""Screenlog 메뉴바 앱

녹화기(screenpipe, launchd로 등록됨)를 시작/중지하고 상태를 보여준다.
검색/질문은 여기서 안 한다 — 그건 웹(screenlog)이 담당한다. 이 앱은
"로컬 녹화기 스위치"로 역할을 좁힌다.
"""

import os
import sqlite3
import subprocess
from pathlib import Path

import rumps

LABEL = "com.screenlog.recorder"
PLIST_PATH = Path.home() / "Library/LaunchAgents/com.screenlog.recorder.plist"
DATA_DIR = Path.home() / ".screenpipe-redacted"
DB_PATH = DATA_DIR / "db.sqlite"

# 동기화(색인 + EC2 전송) 대상. 팀원 배포판에서는 이 값들을 설정 파일로
# 빼야 하지만, 지금은 개인 사용 기준으로 고정값을 쓴다.
SCREENLOG_DIR = Path.home() / "screenlog"
EC2_HOST = "15.164.242.189"
EC2_USER = "ubuntu"
EC2_KEY = Path.home() / "Downloads/EXPRESS-BEC.pem"
DASHBOARD_URL = "http://15.164.242.189:8000"


def is_running() -> bool:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True
    )
    return any(LABEL in line for line in result.stdout.splitlines())


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
        self.status_item = rumps.MenuItem("상태 확인 중...")
        self.storage_item = rumps.MenuItem("저장 용량: -")
        self.redaction_item = rumps.MenuItem("리덕션: -")
        self.toggle_item = rumps.MenuItem("시작/중지", callback=self.toggle_recording)
        self.sync_item = rumps.MenuItem("지금 동기화 (색인 + 서버 전송)", callback=self.sync_now)
        self.dashboard_item = rumps.MenuItem("웹 대시보드 열기", callback=self.open_dashboard)

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            None,
            self.storage_item,
            self.redaction_item,
            None,
            self.sync_item,
            self.dashboard_item,
        ]

        self.refresh(None)
        self.timer = rumps.Timer(self.refresh, 15)
        self.timer.start()

    def refresh(self, _):
        running = is_running()
        self.title = "●" if running else "○"
        self.status_item.title = "● 녹화 중" if running else "○ 꺼짐"
        self.toggle_item.title = "중지" if running else "시작"
        self.storage_item.title = f"저장 용량: {storage_used()}"
        self.redaction_item.title = f"리덕션: {redaction_progress()}"

    def toggle_recording(self, _):
        if is_running():
            subprocess.run(["launchctl", "unload", str(PLIST_PATH)])
        else:
            subprocess.run(["launchctl", "load", str(PLIST_PATH)])
        self.refresh(None)

    def sync_now(self, _):
        rumps.notification("Screenlog", "동기화 시작", "색인 중... (몇 분 걸릴 수 있음)")

        index_cmd = ["uv", "run", "python", "-m", "screenlog.index"]
        result = subprocess.run(index_cmd, cwd=str(SCREENLOG_DIR), capture_output=True, text=True)
        if result.returncode != 0:
            rumps.notification("Screenlog", "색인 실패", result.stderr[-200:] or "알 수 없는 오류")
            return

        rsync_cmd = [
            "rsync", "-az", "-e", f"ssh -i {EC2_KEY}",
            f"{SCREENLOG_DIR}/chroma/", f"{EC2_USER}@{EC2_HOST}:~/screenlog/chroma/",
        ]
        result = subprocess.run(rsync_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            rumps.notification("Screenlog", "서버 전송 실패", result.stderr[-200:] or "알 수 없는 오류")
            return

        rumps.notification("Screenlog", "동기화 완료", "서버에 최신 데이터가 반영됐습니다.")
        self.refresh(None)

    def open_dashboard(self, _):
        os.system(f"open {DASHBOARD_URL}")


if __name__ == "__main__":
    ScreenlogMenuBar().run()
