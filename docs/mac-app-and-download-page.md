# 통합 Mac 앱 + 다운로드 페이지 구축 기록 (2026-08-02)

리덕션(개인정보 자동 마스킹) 적용된 screenpipe를 직접 빌드하고, 이걸 터미널
없이 설치·조작할 수 있는 메뉴바 앱으로 만들어서, `screenlog` 웹서버에서
다운로드까지 되게 만든 과정. [ec2-deployment-guide.md](ec2-deployment-guide.md)의
후속 작업.

## 배경

- 개인정보(이메일, 전화번호, API 키 등)가 화면 기록에 그대로 찍혀 들어가는
  문제를 막기 위해 screenpipe의 리덕션 기능이 필요했음
- screenpipe는 2026-06-10에 MIT → 상업 라이선스(Screenpipe Commercial
  License)로 전환됨. 개인 비상업적 용도는 여전히 무료지만, 지금 설치된
  공식 앱(v2.5.158)은 최신 버전이라 상업 라이선스 대상
- 리덕션 기능(`screenpipe-redact` 크레이트)은 2026-05-06에 추가됐고, 라이선스
  전환은 6/10 — 그 사이 시점에 태그된 **`app-v2.4.311`**(2026-06-03)이
  **"MIT + 리덕션 포함"**인 유일한 조합이라 이걸 소스로 빌드하기로 함
- 시맨틱 컨텍스트 기능(`screenpipe-semantic`)은 7/29 추가 — 상업 라이선스
  전환(6/10) 이후라 MIT로 쓸 수 있는 방법 자체가 없음. 어차피 screenlog
  자체 파이프라인(bge-m3 임베딩 + LLM 라우팅)과 기능이 겹쳐서 불필요

## 1. screenpipe 소스 빌드

```bash
git clone --branch app-v2.4.311 https://github.com/mediar-ai/screenpipe.git ~/screenpipe-vendor
cd ~/screenpipe-vendor
cargo build --release -p screenpipe-engine
```

필요했던 사전 설치:
- Rust (`rustup`)
- Xcode 전체 설치(Command Line Tools만으론 부족 — `cidre` 크레이트가
  `xcodebuild` 호출) + `sudo xcodebuild -runFirstLaunch`
- `cmake` (whisper-rs-sys가 필요)

리덕션 켜서 실행:
```bash
screenpipe record --data-dir ~/.screenpipe-redacted \
  --async-pii-redaction \
  --pii-redaction-labels secret,email,phone,person,address \
  --pii-backend local
```
`--pii-redaction-labels` 기본값이 `secret`뿐이라, 이메일/전화번호까지
가리려면 명시적으로 지정해야 함.

**스키마 호환성**: `app-v2.4.311`은 `screenlog`의 [source.py](../src/screenlog/source.py)가
읽는 8개 컬럼(`id, timestamp, app_name, window_name, browser_url,
capture_trigger, text_source, full_text`)을 전부 갖고 있어 코드 수정 없이
그대로 사용 가능. 단, 리덕션 추적 컬럼명이 최신 버전과 다름
(`accessibility_redacted_at`은 있지만 `full_text_redacted_at`은 없음 — 이건
6/13 이후 추가된 컬럼).

## 2. 앱 형태로 패키징 — 겪은 문제들

처음엔 "녹화기(.dmg)"와 "메뉴바 컨트롤러(.dmg)"를 따로 만들었다가, 사용자
입장에서 "뭘 먼저 받아야 하나" 헷갈려서 **하나의 `.app`으로 통합**
(`py2app`으로 빌드, 녹화기 바이너리를 `Contents/Resources/`에 내장).

### 문제 1 — launchd로 백그라운드 등록하면 권한 팝업 전에 재시작 반복
`KeepAlive: true`로 launchd에 등록했더니, 권한 팝업이 뜨기도 전에 크래시 →
재시작을 반복하다 launchd가 포기(`-6` 종료 코드). **해결**: launchd 대신
메뉴바 앱(GUI 프로세스)이 `subprocess.Popen`으로 직접 자식 프로세스로 띄우게
변경. 사용자가 실제로 더블클릭해서 연 GUI 앱 컨텍스트에서 띄워야 권한
팝업이 정상적으로 뜬다.

### 문제 2 — 마이크 접근 시 조용히 크래시 (TCC)
크래시 리포트(`~/Library/Logs/DiagnosticReports/`)에서 원인 발견:
```
namespace: TCC
details: "The app's Info.plist must contain an NSMicrophoneUsageDescription key..."
```
`py2app`의 `setup.py`에 `NSMicrophoneUsageDescription`,
`NSScreenCaptureUsageDescription`, `NSAppleEventsUsageDescription`을 추가해서
해결. **macOS는 "책임 프로세스"(이 경우 메뉴바 앱)의 Info.plist를 검사한다** —
실제로 마이크를 여는 게 내장된 녹화기 바이너리여도 마찬가지.

### 문제 3 — 접근성 권한을 사용자가 직접 찾아 들어가야 함
`ApplicationServices.AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})`
호출로 macOS 표준 요청 팝업을 자동으로 띄우게 함 — 권한 자체를 코드로 켤 순
없지만(보안상 사용자만 가능), 그 설정 화면으로 자동으로 데려다주는 것까진
가능. `py2app`이 이 pyobjc 프레임워크를 자동으로 못 묶어서
`OPTIONS["packages"] = ["ApplicationServices", "Quartz"]`를 명시해야 했음
(`"includes"`로는 안 됨 — pyobjc 프레임워크는 `"packages"`로 통째로 복사해야
누락 없이 번들링됨).

### 문제 4 — 재빌드할 때마다 권한이 초기화됨
서명 안 된 바이너리는 macOS가 매 빌드를 "새 프로그램"으로 인식해서 권한을
다시 물어봄. `codesign --force --deep -s - Screenlog.app`으로 ad-hoc 서명을
적용해 완화 (완전한 배포용 서명은 Apple Developer 계정 필요, $99/년 — 지금
단계에선 생략).

### 문제 5 — GUI 앱의 stdout/stderr가 `/dev/null`로 감
Finder에서 더블클릭해서 띄운 앱은 표준출력이 버려져서, 백그라운드 작업(동기화
등)이 실패해도 어디에도 기록이 안 남음. 앱 자체 로그 파일
(`~/.screenpipe-redacted/menubar.log`)에 타임스탬프와 함께 직접 기록하고,
메뉴에 "동기화 로그 보기"를 추가해서 사용자가 직접 확인 가능하게 함.

## 3. Gatekeeper 우회 (배포용, Apple 인증 없이)

서명은 ad-hoc(`codesign -s -`)이라 여전히 "확인되지 않은 개발자" 경고가 뜬다.
사용자는 **control+클릭 → 열기**로 한 번만 우회하면 됨 (더블클릭만 하면
계속 막힘). `.dmg`도 진짜 브라우저 다운로드로 받아야 격리(quarantine)
속성이 붙어서 이 경고가 재현됨 — 로컬 파일 복사로는 재현 안 됨.

## 4. 다운로드 페이지 서빙

`api.py`에 정적 파일 마운트 추가:
```python
DOWNLOAD_DIR = Path(__file__).parent.parent.parent / "distribution" / "mac"
app.mount("/download", StaticFiles(directory=DOWNLOAD_DIR, html=True), name="download")
```
로컬이든 EC2든 같은 서버가 다운로드 페이지까지 서빙 — 별도 파일 공유 불필요.

`Dockerfile`에서 `distribution/` 복사를 `uv sync` **뒤**로 옮김 — dmg 파일이
자주 바뀌는데, 의존성 설치 앞에 두면 그때마다 무거운 설치 캐시가 깨짐.

## 5. 웹사이트 구조 분리

기존엔 `/`가 대시보드+챗봇 역할까지 다 했는데, 나중에 로그인을 붙일 걸
대비해서 분리:

```
/              랜딩 페이지 (제품 소개 + 다운로드/대시보드 버튼) — 새로 만듦
/dashboard     통계+타임라인+챗봇 (기존 index.html을 여기로 이동)
/download/     앱 다운로드
```

## 6. GitHub Actions 빌드 캐시

이미지가 8.5GB(임베딩 모델+torch 포함)라 배포마다 오래 걸리는 문제 완화:

```yaml
- name: Set up Docker Buildx
  uses: docker/setup-buildx-action@v3   # 기본 docker 드라이버는 캐시 export 미지원

- name: Build and push image
  uses: docker/build-push-action@v6
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

## 검증 완료된 것

- `screenpipe-vendor` 빌드 → 화면/오디오 캡처 + 리덕션 실제 동작 확인
  (전체 82개 프레임 중 80개 리덕션 처리됨)
- 메뉴바 앱의 "지금 동기화"(색인 + `rsync`)를 터미널로 직접 실행해 검증 —
  로컬 chromadb 17,324개 = EC2 서버 chromadb 17,324개, 8/1·8/2 데이터 포함
  확인

## 아직 안 된 것 (다음 단계 후보)

- 메뉴바 앱의 "지금 동기화" 버튼 클릭 → 로그 기록까지 GUI에서 직접 확인
  (터미널 실행으로는 검증됐지만, 버튼 클릭 경로 자체의 최종 확인 필요)
- 팀원 PC → 서버 자동 업로드용 `/ingest` 엔드포인트 (지금은 로컬에서 수동
  `rsync`만 있음)
- Apple Developer 서명/공증 ($99/년) — 지금은 생략, Gatekeeper 경고 1회
  수동 우회로 대체 중
