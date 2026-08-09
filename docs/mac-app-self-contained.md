# 맥 앱 자립화 — 저장소·SSH 키 없이 dmg 하나로 (2026-08-08)

[mac-app-and-download-page.md](mac-app-and-download-page.md)의 후속 작업.
그때 만든 메뉴바 앱은 녹화는 혼자 했지만, **동기화는 개발자 본인 맥에서만**
동작했다. 이걸 "dmg만 받으면 되는" 상태로 바꾼 과정.

## 배경 — 무엇이 개발자 전용이었나

메뉴바 앱에는 이미 "지금 동기화" 버튼이 있었고, 흐름 자체는 지금과 같았다.

```
녹화 → 이벤트화(clean.py) → 임베딩(index.py) → 서버 반영
```

문제는 이걸 **사용자 맥에 클론된 저장소를 `uv run`으로 실행해서** 했다는
것이다. `app.py`에 이런 상수들이 박혀 있었다.

```python
SCREENLOG_DIR = Path.home() / "screenlog"              # 저장소가 클론돼 있어야 함
UV_BIN        = Path.home() / ".local/bin/uv"          # uv가 깔려 있어야 함
EC2_KEY       = Path.home() / "Downloads/EXPRESS-BEC.pem"   # EC2 개인키가 있어야 함
EC2_HOST      = "54.116.52.38"
```

dmg만 받은 사람에게는 이 중 어느 것도 없다. 실제로 첫 검사에서
`SCREENLOG_DIR 없음`으로 즉시 실패한다.

그리고 전송 방식 자체도 배포가 불가능한 구조였다.

```
맥의 chroma/(688MB) ──rsync(개인키)──> EC2의 chroma/를 통째로 덮어쓰기
                                       ──ssh──> docker compose restart
```

- **개인키를 배포할 수 없다.** dmg에 `.pem`을 넣는 순간 받은 사람 누구나
  EC2에 SSH로 들어갈 수 있다.
- **디렉토리를 통째로 덮는다.** 팀원 두 명이 동기화하면 나중 사람이
  앞사람 데이터를 지운다.
- **재시작이 필요하다.** 서버가 chroma 클라이언트를 프로세스 시작 시점에
  캐싱하므로(`get_collection()`), 파일만 바꿔치기하면 알아채지 못한다.
  그래서 매 동기화마다 컨테이너를 재시작했고, 그동안 다른 사람은 서비스를
  못 썼다.

---

## 1. 전송을 rsync에서 HTTP 인제스트로

서버에 엔드포인트 두 개를 추가하고([api.py](../src/screenlog/api.py)),
클라이언트는 벡터를 HTTP로 올린다.

| 엔드포인트 | 역할 |
|---|---|
| `POST /api/ingest` | (id, 벡터, 본문, 메타데이터) 배치를 chroma에 upsert |
| `POST /api/ingest/known` | 이 id들 중 서버에 이미 있는 것을 돌려줌 |

세 문제가 한꺼번에 없어진다. 개인키가 필요 없고(기존 Basic Auth 재사용),
덮어쓰기가 아니라 upsert라 사용자끼리 안 부딪히고, 서버가 자기 손으로 넣으니
재시작이 불필요하다.

### 멱등성은 공짜로 얻었다

id를 클라이언트가 정하는데, 그게 `index.event_id()`가 만드는 내용 해시
(`app|window|start|text`의 sha1)다. 같은 이벤트는 몇 번을 보내도 같은 id가
나오므로 재전송해도 중복이 안 쌓이고 덮어쓰기만 된다.

### `/api/ingest/known`이 따로 필요한 이유

이 파이프라인에서 제일 비싼 단계는 임베딩이다(하루치가 수 분). 예전엔
클라이언트가 로컬 chroma를 들고 있어서 "이미 넣었나"를 자기 자신에게
물어볼 수 있었지만, 이제 진실의 원본은 서버 한 곳뿐이다. 임베딩을
시작하기 **전에** 물어봐야 두 번째 동기화부터 통째로 낭비하지 않는다.

### 서버가 차원을 직접 검사한다

클라이언트가 만든 벡터를 그대로 받아 넣기 때문에, 차원이 다른 벡터가 한 번
섞여 들어가면 컬렉션 전체의 검색이 깨진다. 그리고 그 시점엔 **어느 게 잘못
들어간 건지 구분할 방법이 없다.** 그래서 `EMBED_DIM`(1024)과 다르면 422로
막는다 — 클라이언트와 서버의 임베딩 모델이 어긋났다는 신호다.

---

## 2. 로컬 chroma를 아예 없앴다

새로 만든 [sync.py](../src/screenlog/sync.py)는 로컬에 chroma를 두지 않는다.
같은 데이터를 두 벌 들고 있을 이유가 없어서다. 대신 **이미 보낸 id만** 작은
sqlite(`sent_events.sqlite`)에 적는다.

이 파일은 서버에 물어보는 왕복을 아끼려는 캐시일 뿐이라, 지워도 다시
물어보면 그만이다. 정확성은 서버 쪽 upsert 멱등성이 보장한다.

> 실측: `sent_events.sqlite`를 지우고 다시 동기화하면 서버에 물어본 뒤
> **1.3초 만에 0건 업로드**로 끝난다. 재임베딩이 일어나지 않는다.

기록 순서도 중요하다. **전송이 확인된 뒤에만** id를 적는다. 반대로 하면
전송이 실패한 이벤트가 "보냄"으로 남아서 영영 안 올라간다.

---

## 3. 같은 패키지가 두 역할을 하게 됐다

이제 `screenlog` 패키지가 EC2 서버와 맥 앱 양쪽에서 돈다. 요구사항이 다르다.

| | 서버 | 맥 앱 |
|---|---|---|
| LLM 키 | 필요 | **불필요** (질문에 답하지 않음) |
| `SCREENLOG_USER/PASSWORD` | 서버를 잠그는 열쇠 | 서버에 로그인할 계정 |
| chroma | 필요 | **불필요** |

`SCREENLOG_ROLE=client`로 이걸 가른다([config.py](../src/screenlog/config.py)).

문제가 됐던 건 두 검사가 **import 시점에** 있었다는 점이다.

- LLM 키가 없으면 `RuntimeError` → 키 없는 사용자 맥에서 `screenlog.index`를
  불러오는 것만으로 앱이 죽었다.
- 계정이 없으면 `RuntimeError` → 앱은 사용자가 계정을 **입력하기 전에** 일단
  떠서 설정 창을 보여줘야 하는데, 기동 자체가 막혔다.

검사를 그냥 없애지는 않았다. 서버가 키 없이 떴을 때 한참 뒤 엉뚱한 곳에서
터지는 게 더 나쁘기 때문에, **클라이언트라고 명시한 경우에만 면제**한다.
서버 동작은 종전과 완전히 동일하다.

### `CHROMA_DIR`이 상대 경로였던 게 저장소 의존의 근본 원인

```python
CHROMA_DIR = "chroma"    # 현재 디렉토리 기준
```

앱이 굳이 `cd ~/screenlog`를 한 뒤 `uv run`을 해야 했던 이유가 이것이다.
환경변수(`SCREENLOG_DATA_DIR`)로 덮어쓸 수 있게 열되, **기본값은 상대 경로
그대로** 뒀다 — 서버(Docker)는 `WORKDIR=/app`에 chroma를 볼륨으로 붙이므로
기본값을 바꾸면 배포가 깨진다.

파생 경로(`chat_history.sqlite`, `summary_cache.sqlite`)도 이 값을 따라간다.

---

## 4. 모델은 번들에 넣지 않고 첫 실행에 받는다

`bge-m3` 가중치가 2.1GB다. 선택지를 재보면:

| 방식 | dmg 크기 | 첫 실행 |
|---|---|---|
| 통째로 번들 | ~2.8GB | 즉시 동작 |
| **첫 실행 다운로드** | ~485MB | 2.1GB 받는 동안 대기 |
| ONNX + 양자화 | ~250MB | 벡터가 바뀜 → 전체 재색인 필요 |

**첫 실행 다운로드**를 택했다. 이유는 용량 자체보다 배포 파이프라인이다.

- 애플 공증(notarization)에 몇 GB를 업로드·스캔받아야 한다
- torch 안의 수천 개 `.dylib`을 전부 코드 서명해야 한다
- 앱 코드 한 줄 고쳐 재배포할 때마다 사용자가 2.8GB를 다시 받는다
- 모델을 따로 두면 앱을 업데이트해도 캐시된 모델은 그대로 재사용된다

그리고 **이 프로젝트가 이미 이 방식을 쓰고 있다.** 내장된 `screenpipe-bin`은
53MB인데 whisper 모델은 첫 실행에 받는다.

### 받는 양을 절반으로 줄였다

`bge-m3` 저장소를 통째로 받으면 4.3GB인데, 그중 우리가 쓰는 건 절반뿐이다.

```python
MODEL_IGNORE_PATTERNS = ["onnx/*", "imgs/*", "*.jpg", "*.webp",
                         "*_linear.pt", ".gitattributes"]
```

- `onnx/` — onnxruntime용 사본(`model.onnx_data`만 2GB대). 우리는 torch로 돈다
- `*_linear.pt` — FlagEmbedding의 colbert/sparse 헤드. dense 임베딩만 쓰는 우리와 무관
- `imgs/` — README에 박힌 벤치마크 그림

사용자 회선으로 받는 것이라, 안 쓰는 2GB를 받게 두면 첫 동기화까지의 대기가
그냥 두 배가 된다.

---

## 5. 앱이 사용자에게 물어보는 것

계정이 사람마다 다르므로 코드에 박을 수 없다. 메뉴에 **서버 설정**을 넣고
`~/.screenpipe-redacted/config.json`에 `0600` 권한으로 저장한다.

동기화 시점에 이 값을 환경변수로 넣는데, **반드시 `import`보다 먼저** 채워야
한다 — `screenlog.config`가 import 시점에 환경변수를 읽기 때문에, 순서가
바뀌면 사용자가 방금 입력한 계정 대신 빈 값이 박힌 채로 굳는다.

```python
os.environ["SCREENLOG_ROLE"] = "client"
os.environ["SCREENLOG_SERVER_URL"] = ...
os.environ["SCREENLOG_USER"] = ...
# 그 다음에야
from screenlog.sync import sync_all
```

---

## 6. py2app 번들 구성

`setup.py`에 `screenlog`와 임베딩 의존성을 넣고, 서버 전용은 제외한다.

```python
"packages": ["screenlog", "sentence_transformers", "transformers", "torch", ...],
"excludes": ["chromadb", "onnxruntime", "fastapi", "uvicorn", "langgraph", ...],
```

`chromadb`를 제외하려면 [index.py](../src/screenlog/index.py)의
`import chromadb`를 모듈 최상단에서 `get_collection()` 안으로 내려야 했다.
최상단에 두면 클라이언트가 안 쓰는데도 onnxruntime까지 딸려 들어간다.

---

## 검증 완료된 것

번들된 파이썬으로 직접 확인했다.

```bash
cd distribution/mac/menubar/dist/Screenlog.app/Contents
export RESOURCEPATH="$PWD/Resources"
export PYTHONPATH="$PWD/Resources/lib/python3.14:$PWD/Resources/lib/python314.zip:\
$PWD/Resources/lib/python3.14/lib-dynload:$PWD/Resources"
MacOS/python <검증스크립트>
```

| 항목 | 결과 |
|---|---|
| 번들 안 `screenlog` import | OK |
| 번들 안 torch + bge-m3 임베딩 | OK (1024차원) |
| 서버 전용 모듈 제외 | chromadb·onnxruntime·fastapi·uvicorn·langgraph 전부 빠짐 |
| 실데이터 동기화 | 7일치 300건 업로드 → 서버에서 검색됨 |
| 인제스트 API | 인증(401) / 멱등성 / 차원검증(422) / 중복 접기 / 배치 상한(413) / 실검색 |
| `sent_events.sqlite` 삭제 후 재동기화 | 1.3초, 0건 (재임베딩 없음) |
| 죽은 IP 잔존 | 0곳 |

**산출물**: `distribution/mac/Screenlog.dmg` — 485MB (`.app` 1.0GB, 체크섬 검증 통과)

---

## 아직 안 된 것

### 서버가 평문 HTTP다 (배포 전 처리 필요)

`http://3.35.7.225:8000`은 HTTPS가 아니다. HTTP Basic 인증은 자격증명을
base64로 인코딩할 뿐 암호화하지 않으므로, 같은 네트워크에 있는 누구나
아이디/비밀번호를 읽을 수 있다. 이번 변경으로 `/api/ingest`를 통해 **화면
기록 본문까지** 평문으로 오간다.

팀원에게 dmg를 배포하기 전에 리버스 프록시(Caddy/nginx + Let's Encrypt)나
ALB + ACM으로 HTTPS를 붙여야 한다.

### 번들 크기

`.app`이 1.0GB인데 상당 부분이 추론에 안 쓰인다 — `torch/include`(C++ 헤더
9,976개, 61MB), `torch/_inductor`(23MB), sympy(76MB), scipy(95MB).
제거할 때마다 위 검증 절차를 다시 돌려야 안전하다.

### 요약 캐시 동기화가 빠졌다

예전 rsync 경로는 `summary_cache.sqlite`도 같이 넘겼다(chroma/ 안에 있어서).
지금은 이벤트 벡터만 올린다. 요약이 없으면 서버가 그때그때 만드는 예전
방식으로 자동 대체되므로 기능이 깨지지는 않지만, 대시보드 첫 로딩이
느려질 수 있다.

### 애플 공증은 여전히 안 받았다

Gatekeeper 우회 절차(control+클릭 → 열기)가 그대로 필요하다.
[mac-app-and-download-page.md](mac-app-and-download-page.md) 참고.
