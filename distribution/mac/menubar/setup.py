"""py2app 빌드 설정.

빌드: source .venv/bin/activate && python3 setup.py py2app
"""

import sys

from setuptools import setup

# py2app은 번들에 넣을 모듈을 찾으려고 소스를 전부 AST로 훑는다. torch에는
# 그 방식으로는 너무 깊게 중첩된 표현식이 있어서 기본 재귀 한도(1000)로는
# 분석 도중 RecursionError로 빌드가 통째로 죽는다(실측). 한도만 올려주면
# 통과한다 — 재귀가 무한한 게 아니라 그냥 깊은 것뿐이다.
sys.setrecursionlimit(10_000)

APP = ["app.py"]
OPTIONS = {
    "argv_emulation": False,
    "resources": ["resources/screenpipe-bin"],
    # screenlog와 그 임베딩 의존성을 앱 안에 넣는다. 예전엔 앱이 사용자
    # 맥에 클론된 ~/screenlog를 `uv run`으로 실행했는데, dmg만 받은
    # 사람에겐 그 저장소도 uv도 없다.
    #
    # 모델 가중치(bge-m3, 2.1GB)는 여기 안 넣는다 — 첫 실행 때
    # screenlog.index.ensure_model()이 받는다. 번들에 넣으면 dmg가
    # 2.8GB가 되어 애플 공증부터 재배포까지 전 과정이 무거워진다.
    "packages": [
        "ApplicationServices", "Quartz",
        "screenlog",
        "sentence_transformers", "transformers", "torch", "tokenizers",
        "huggingface_hub", "safetensors", "httpx", "dotenv",
    ],
    # torch/transformers는 지연 임포트가 많아서 py2app의 정적 분석이
    # 놓치는 모듈이 생긴다. 빌드는 되는데 실행 중에 ModuleNotFoundError가
    # 나는 유형이라 명시적으로 끌어온다.
    "includes": ["screenlog.sync", "screenlog.index", "screenlog.clean"],
    # chromadb는 서버 전용이다. 앱은 벡터를 만들어 HTTP로 올릴 뿐이고
    # 로컬에 chroma를 두지 않으므로(screenlog/sync.py 참고) 넣지 않는다 —
    # 넣으면 onnxruntime까지 딸려와서 번들만 커진다.
    "excludes": ["chromadb", "onnxruntime", "fastapi", "uvicorn", "langgraph",
                 "langchain_core", "langchain_openai"],
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
