"""py2app 빌드 설정.

빌드: source .venv/bin/activate && python3 setup.py py2app
"""

from setuptools import setup

APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Screenlog",
        "CFBundleDisplayName": "Screenlog",
        "CFBundleIdentifier": "com.screenlog.menubar",
        "CFBundleShortVersionString": "1.0",
        "LSUIElement": True,  # 독(Dock)에 아이콘 안 뜨고 메뉴바에만 뜨게
    },
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
