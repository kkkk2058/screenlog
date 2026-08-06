# EC2 배포 가이드 (2026-07-31)

Docker화 + EC2 인스턴스 + GitHub Actions CI/CD로 배포한 전체 과정 기록.
나중에 새 프로젝트나 재배포할 때 이 순서 그대로 따라하면 된다.

## 0. 사전 준비물

- `Dockerfile` — 앱을 이미지로 빌드하는 정의
- `docker-compose.yml` — 로컬 개발용 (`build: .`로 직접 빌드)
- `.env` — API 키 등 환경변수 (git에 커밋하지 않음)

---

## 1. GitHub Actions 워크플로우 작성

`.github/workflows/deploy.yml`: main 브랜치에 push하면 자동으로 두 단계가 돈다.

**1단계 (build-and-push)**: 이미지를 빌드해서 GHCR(GitHub Container Registry)에 push
```yaml
- uses: docker/build-push-action@v6
  with:
    context: .
    push: true
    tags: |
      ghcr.io/${{ github.repository }}:latest
      ghcr.io/${{ github.repository }}:${{ github.sha }}
```

**2단계 (deploy)**: `appleboy/ssh-action`으로 EC2에 SSH 접속해서 재배포
```yaml
- uses: appleboy/ssh-action@v1
  with:
    host: ${{ secrets.EC2_HOST }}
    username: ${{ secrets.EC2_USER }}
    key: ${{ secrets.EC2_SSH_KEY }}
    script: |
      cd /home/ubuntu/screenlog   # AMI가 Ubuntu면 유저는 ubuntu, Amazon Linux면 ec2-user
      echo "${{ secrets.GITHUB_TOKEN }}" | docker login ghcr.io -u ${{ github.actor }} --password-stdin
      docker compose pull
      docker compose up -d
      docker image prune -f
```

> **주의**: `host`/`username`/`key`가 비어있으면 `missing server host` 에러가 난다 —
> 3번(시크릿 등록)을 안 했을 때 가장 먼저 겪는 에러.

---

## 2. EC2 인스턴스 생성 (AWS 콘솔)

1. **AMI**: Ubuntu Server 24.04 LTS (HVM), 64비트(x86)
2. **인스턴스 유형**: 사양은 서비스 부하로 결정 — 이번엔 임베딩 모델(BAAI/bge-m3,
   CPU 추론) + chromadb 감안해서 **t3.large (2 vCPU, 8GB RAM)** 선택.
   - 서버가 질의만 서빙하고 색인은 로컬(GPU)에서 한다면 t3.large로 충분
   - 서버에서 색인까지 상시 돌린다면 t3.xlarge(4 vCPU, 16GB) 이상 권장
3. **키 페어**: 새로 생성 → `.pem` 다운로드 (예: `EXPRESS-BEC.pem`) — 이게 SSH 접속과
   `EC2_SSH_KEY` 시크릿에 그대로 쓰인다.
4. **스토리지**: 기본 8GB보다 늘려서 20~30GB (색인 데이터가 계속 쌓이므로)
5. **보안 그룹(인바운드)**: 최소 두 개 열어야 한다.
   - SSH(22) — 접속용
   - 앱 포트(예: 8000, 커스텀 TCP) — 실제 서비스용
   - **인스턴스에 보안 그룹을 여러 개 붙일 수 있다.** 나중에 포트를 추가로 열 때
     기존 보안그룹을 편집하거나, 새 보안그룹을 만들어서 인스턴스에 추가 연결하면 됨
     (작업 → 보안 → 보안 그룹 변경).

---

## 3. 로컬에서 EC2로 접속 확인

```bash
chmod 600 ~/Downloads/EXPRESS-BEC.pem   # 권한 0644면 "bad permissions"로 거부됨
ssh -i ~/Downloads/EXPRESS-BEC.pem ubuntu@<퍼블릭 IP>
```

흔한 실패 원인:
- 키 파일 권한이 너무 열려있음 (`chmod 600` 필요)
- 인스턴스 생성 시 선택한 키 페어 이름과 실제로 갖고 있는 `.pem` 파일이 다름
- 보안그룹에 22번 포트가 안 열려있음

---

## 4. EC2에 Docker 설치

Ubuntu 저장소의 `docker-compose-plugin`은 실제 패키지명이 아니라서
(`docker-compose-v2`가 맞는 이름) apt로 바로 설치하면 실패한다.
**Docker 공식 설치 스크립트**를 쓰는 게 확실하다.

```bash
ssh -i ~/Downloads/EXPRESS-BEC.pem ubuntu@<IP> \
  "curl -fsSL https://get.docker.com | sudo sh && \
   sudo systemctl enable --now docker && \
   sudo usermod -aG docker ubuntu && \
   docker --version && docker compose version"
```

---

## 5. EC2 전용 docker-compose 파일 작성

로컬용(`docker-compose.yml`)은 `build: .`로 직접 빌드하지만, EC2는 GHCR에서
이미 빌드된 이미지를 받아야 하므로 별도 파일이 필요하다.

`docker-compose.prod.yml`:
```yaml
services:
  screenlog:
    image: ghcr.io/<github계정>/<repo명>:latest
    ports:
      - "8000:8000"
    volumes:
      - ~/.screenpipe/db.sqlite:/root/.screenpipe/db.sqlite:ro
      - ./chroma:/app/chroma
    env_file:
      - .env
    restart: unless-stopped
```

> 이미지 경로는 `git remote get-url origin` (로컬 저장소에서!) 결과인
> `https://github.com/<owner>/<repo>`에서 `ghcr.io/<owner>/<repo>:latest`로 만든다.
> GitHub Actions 안에서는 `${{ github.repository }}`가 자동으로 이 값이 된다.

---

## 6. 파일 서버로 전송

```bash
ssh -i ~/Downloads/EXPRESS-BEC.pem ubuntu@<IP> "mkdir -p ~/screenlog"
scp -i ~/Downloads/EXPRESS-BEC.pem docker-compose.prod.yml ubuntu@<IP>:~/screenlog/docker-compose.yml
scp -i ~/Downloads/EXPRESS-BEC.pem .env ubuntu@<IP>:~/screenlog/.env
rsync -avz -e "ssh -i ~/Downloads/EXPRESS-BEC.pem" ./chroma/ ubuntu@<IP>:~/screenlog/chroma/
```

(색인은 로컬 GPU/MPS에서 미리 끝내고, 결과물인 `chroma/` 폴더만 동기화하는 구조.
EC2에서 처음부터 재색인하면 CPU라 훨씬 느리다.)

---

## 7. GitHub 시크릿 등록

```bash
gh secret set EC2_HOST --body "<퍼블릭 IP>"
gh secret set EC2_USER --body "ubuntu"
gh secret set EC2_SSH_KEY < ~/Downloads/EXPRESS-BEC.pem
```

---

## 8. 배포 트리거

```bash
git add .github/workflows/deploy.yml docker-compose.prod.yml
git commit -m "fix: EC2 배포 설정"
git push
```

push하면 Actions 탭에서 build-and-push → deploy 순으로 자동 실행된다.
(이미지에 임베딩 모델+torch가 포함되면 레이어가 5GB 넘게 나올 수 있어
push에 몇 분 걸리는 게 정상.)

---

## 9. 배포 확인

```bash
# 1) GitHub Actions 성공 여부
gh run list --workflow=deploy.yml --limit 3

# 2) 서버 내부에서 컨테이너 상태
ssh -i ~/Downloads/EXPRESS-BEC.pem ubuntu@<IP> \
  "cd ~/screenlog && docker compose ps && docker compose logs --tail=30"

# 3) 외부에서 실제 접속 확인
curl -i http://<IP>:8000
```

`curl`이 타임아웃 나는데 서버 내부(`curl localhost:8000`)는 200이 뜬다면
**보안그룹에 앱 포트가 안 열려있거나, 새로 만든 보안그룹을 인스턴스에
연결(attach)만 하고 실제로 붙이지 않은 경우**다. 인스턴스 상세 →
보안 탭에서 어떤 보안그룹이 실제로 붙어있는지 확인할 것.

---

## 10. 운영/비용 관리

- **비용 확인**: 콘솔 우측 상단 → Billing and Cost Management → Cost Explorer.
  AWS CLI로 보려면 `aws sts get-caller-identity`로 자격증명부터 확인.
- **t3 계열은 버스터블 인스턴스**: 평소 CPU를 적게 쓰면 크레딧이 쌓이고,
  갑자기 부하가 몰리면 그 크레딧을 소모해서 순간적으로 100%까지 쓸 수 있다.
  크레딧이 바닥나면 베이스라인 성능으로 강제 제한(스로틀링)된다.
  EC2 콘솔의 모니터링 탭에서 "CPU 크레딧 밸런스" 그래프로 확인 가능.
- **중지(Stop) vs 종료(Terminate)**:
  - 중지: 컴퓨팅 비용은 안 나가고 EBS 볼륨 비용만 청구(며칠 단위로는 거의 무시할
    수준). 재시작하면 퍼블릭 IP가 바뀌므로 `EC2_HOST` 시크릿도 다시 등록해야 함.
  - 종료: EBS까지 삭제되어 비용 0원이지만, 재사용하려면 인스턴스를 처음부터
    다시 만들어야 함.
- **EC2 콘솔에서 상태 확인하는 방법**:
  - **연결(Connect) → EC2 Instance Connect**: 키 파일 없이 브라우저에서 바로 터미널 접속
  - **모니터링 탭**: CPU/네트워크/CPU 크레딧 그래프
  - **작업 → 모니터링 및 문제 해결 → 시스템 로그/화면 캡처**: SSH 자체가 안 될 때 디버깅용

---

## 겪었던 문제 요약 (재발 방지용)

| 증상 | 원인 | 해결 |
|---|---|---|
| `missing server host` | GitHub 시크릿(`EC2_HOST` 등) 미등록 | 시크릿 등록 |
| SSH `Permission denied (publickey)` | 키 파일 이름/권한 문제 | 올바른 `.pem` 확인 + `chmod 600` |
| `E: Unable to locate package docker-compose-plugin` | Ubuntu 저장소엔 그 이름의 패키지가 없음 | `get.docker.com` 공식 스크립트 사용 |
| `git remote get-url origin` → `not a git repository` | EC2 서버 안에서 실행함 (로컬에서 해야 함) | 로컬 프로젝트 폴더에서 실행 |
| `curl`이 외부에서 타임아웃, 서버 내부는 200 | 보안그룹에 앱 포트 안 열림 / 새 보안그룹을 인스턴스에 안 붙임 | 인스턴스에 보안그룹 추가 연결 |
| `aws ce get-cost-and-usage` → `UnrecognizedClientException` | 로컬 AWS CLI 자격증명 무효/만료 | 콘솔에서 직접 확인하거나 `aws configure` 재설정 |
