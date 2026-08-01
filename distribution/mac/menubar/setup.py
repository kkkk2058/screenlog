"""py2app 빌드 설정.

빌드: source .venv/bin/activate && python3 setup.py py2app
"""

from setuptools import setup

APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "resources": ["resources/screenpipe-bin"],
    "packages": ["ApplicationServices", "Quartz"],
    "plist": {
        "CFBundleName": "Screenlog",
        "CFBundleDisplayName": "Screenlog",
        "CFBundleIdentifier": "com.screenlog.menubar",
        "CFBundleShortVersionString": "1.0",
        "LSUIElement": True,  # 독(Dock)에 아이콘 안 뜨고 메뉴바에만 뜨게

        # 이게 없으면 내장된 녹화기가 마이크에 접근하려는 순간 macOS가
        # 권한을 묻지도 않고 프로세스를 강제 종료한다(TCC 크래시).
        # "책임 프로세스"인 이 앱(Screenlog.app)의 Info.plist에 이유가
        # 있어야 한다 — 실제로 마이크를 여는 건 내장된 screenpipe-bin이지만,
        # TCC는 이 wrapper 앱 기준으로 검사한다.
        "NSMicrophoneUsageDescription":
            "화면과 함께 시스템/마이크 오디오를 기록해서 검색 가능한 활동 기록을 만듭니다.",
        "NSScreenCaptureUsageDescription":
            "화면 활동을 기록해서 검색 가능한 활동 기록을 만듭니다.",
        "NSAppleEventsUsageDescription":
            "다른 앱의 창 정보를 읽어 어떤 활동이었는지 기록합니다.",
    },
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
