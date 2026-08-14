#!/usr/bin/env python3
"""architecture.svg 생성기 — 로고 중심 배치도.

텍스트 카드가 아니라 **로고가 주인공**이고 그 아래 짧은 캡션만 붙는 형식이다.
서버 경계는 흰 배경 + 얇은 테두리 사각형으로 그리고, 제목은 상자 위에,
호스팅 로고(EC2/Apple)는 상자 테두리에 걸쳐 놓는다.

로고를 외부 URL로 참조하면 GitHub README에서 안 뜬다(SVG 안의 외부 리소스는
차단된다). 그래서 아이콘 path를 파일에 인라인으로 넣어 자기완결형으로 만든다.

아이콘 원본: https://unpkg.com/simple-icons@13/icons/<name>.svg  (CC0-1.0)
VS Code·screenpipe·ChromaDB는 아이콘셋에 없어서 글리프를 직접 그린다.

사용:
    python3 docs/images/src/make_architecture.py docs/images/src/icons
"""
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "architecture.svg"

COLOR = {
    "apple": "#000000", "python": "#3776AB", "docker": "#2496ED",
    "github": "#181717", "githubactions": "#2088FF", "fastapi": "#009688",
    "amazonec2": "#FF9900", "sqlite": "#003B57", "huggingface": "#FFD21E",
    "ollama": "#000000", "openai": "#412991", "anthropic": "#D97757",
    "langchain": "#1C3C3C", "googlechrome": "#4285F4",
}

W, H = 1340, 720
INK = "#111827"          # 테두리·화살표 (참고 배치도처럼 거의 검정)


def load_icons(d: Path) -> dict:
    icons = {}
    for name in COLOR:
        f = d / f"{name}.svg"
        if not f.exists():
            print(f"  ! {name}.svg 없음", file=sys.stderr)
            continue
        m = re.search(r'<path[^>]*\sd="([^"]+)"', f.read_text(encoding="utf-8"))
        if m:
            icons[name] = m.group(1)
    return icons


# ── 그리기 helper ────────────────────────────────────────────────────
def logo(icons, name, cx, cy, size, cap=None, cap2=None):
    """(cx,cy)를 중심으로 로고를 놓고 그 아래 캡션을 단다."""
    out = []
    if name in icons:
        s = size / 24
        out.append(f'<g transform="translate({cx - size / 2},{cy - size / 2}) scale({s:.4f})" '
                   f'fill="{COLOR[name]}"><path d="{icons[name]}"/></g>')
    if cap:
        out.append(f'<text x="{cx}" y="{cy + size / 2 + 17}" text-anchor="middle" class="cap">{cap}</text>')
    if cap2:
        out.append(f'<text x="{cx}" y="{cy + size / 2 + 32}" text-anchor="middle" class="cap2">{cap2}</text>')
    return "".join(out)


def box(x, y, w, h, dash=None, fill="#FFFFFF", stroke=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{fill}" '
            f'stroke="{stroke or INK}" stroke-width="1.6"{d}/>')


def arrow(x1, y1, x2, y2, label=None, lx=None, ly=None, both=False):
    m = ' marker-start="url(#a)"' if both else ""
    out = [f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{INK}" '
           f'stroke-width="1.8" marker-end="url(#a)"{m}/>']
    if label:
        out.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" class="edge">{label}</text>')
    return "".join(out)


def glyph_screen(cx, cy, s=1.0):
    """screenpipe — 모니터 + 녹화점."""
    w, h = 46 * s, 32 * s
    return (f'<rect x="{cx - w / 2}" y="{cy - h / 2}" width="{w}" height="{h}" rx="4" fill="none" '
            f'stroke="{INK}" stroke-width="2.4"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{7 * s}" fill="#DC2626"/>'
            f'<line x1="{cx - 10 * s}" y1="{cy + h / 2 + 7 * s}" x2="{cx + 10 * s}" y2="{cy + h / 2 + 7 * s}" '
            f'stroke="{INK}" stroke-width="2.4"/>'
            f'<line x1="{cx}" y1="{cy + h / 2}" x2="{cx}" y2="{cy + h / 2 + 7 * s}" stroke="{INK}" stroke-width="2.4"/>')


def glyph_chroma(cx, cy):
    """ChromaDB — 브랜드 컬러 원 3개."""
    return "".join(f'<circle cx="{cx - 20 + i * 20}" cy="{cy}" r="12" fill="{c}"/>'
                   for i, c in enumerate(("#FF6B6B", "#FFD93D", "#6BCB77")))


def glyph_dev(cx, cy):
    """개발자 — 노트북 앞 사람(참고 배치도의 Dev 그림 대용)."""
    return (f'<circle cx="{cx}" cy="{cy - 16}" r="9" fill="none" stroke="{INK}" stroke-width="2.2"/>'
            f'<path d="M{cx - 15},{cy + 2} a15,13 0 0,1 30,0" fill="none" stroke="{INK}" stroke-width="2.2"/>'
            f'<rect x="{cx - 22}" y="{cy + 6}" width="44" height="20" rx="2.5" fill="none" stroke="{INK}" stroke-width="2.2"/>'
            f'<line x1="{cx - 30}" y1="{cy + 30}" x2="{cx + 30}" y2="{cy + 30}" stroke="{INK}" stroke-width="2.4"/>')


def glyph_menubar(cx, cy):
    """메뉴바 앱 — 상단 바 + 아이콘."""
    return (f'<rect x="{cx - 26}" y="{cy - 18}" width="52" height="36" rx="4" fill="none" stroke="{INK}" stroke-width="2.2"/>'
            f'<line x1="{cx - 26}" y1="{cy - 7}" x2="{cx + 26}" y2="{cy - 7}" stroke="{INK}" stroke-width="2.2"/>'
            f'<circle cx="{cx + 17}" cy="{cy - 12.5}" r="2.6" fill="{INK}"/>'
            f'<circle cx="{cx + 9}" cy="{cy - 12.5}" r="2.6" fill="{INK}"/>')


# ── 본체 ────────────────────────────────────────────────────────────
def build(icons: dict) -> str:
    p, a = [], None
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             'font-family="-apple-system, BlinkMacSystemFont, \'Apple SD Gothic Neo\', \'Noto Sans KR\', '
             '\'Malgun Gothic\', sans-serif">')
    p.append('''<defs>
    <marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#111827"/></marker>
    <style>
      .ttl {font-size:15px;font-weight:700;fill:#111827}
      .cap {font-size:12px;font-weight:600;fill:#111827}
      .cap2{font-size:10.5px;fill:#6B7280}
      .edge{font-size:11.5px;font-weight:700;fill:#111827}
      .mono{font-family:'SFMono-Regular',Consolas,monospace}
    </style></defs>''')
    p.append(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')
    a = p.append

    # ══ ① Mac — 로컬 ══════════════════════════════════════════════
    a('<text x="250" y="44" text-anchor="middle" class="ttl">Mac — 로컬 (macOS)</text>')
    a(box(40, 62, 420, 300))
    a(logo(icons, "apple", 96, 62, 40))
    a('<text x="96" y="98" text-anchor="middle" class="cap">macOS</text>')

    a(glyph_screen(128, 168))
    a('<text x="128" y="205" text-anchor="middle" class="cap">screenpipe</text>')
    a('<text x="128" y="220" text-anchor="middle" class="cap2">녹화 · PII 리덕션</text>')

    a(logo(icons, "python", 250, 168, 46, "clean.py", "프레임 → 이벤트"))
    a(logo(icons, "huggingface", 378, 168, 50, "BGE-M3", "임베딩 · MPS"))

    a(arrow(160, 168, 222, 168))
    a(arrow(280, 168, 348, 168))

    # 세 단계를 메뉴바 앱이 순서대로 돌린다 — 캡션을 관통하지 않게 아래로 브래킷을 두른다
    a(f'<path d="M128,258 L128,248 M250,258 L250,248 M378,258 L378,248" stroke="{INK}" stroke-width="1.8"/>')
    a(f'<line x1="128" y1="258" x2="378" y2="258" stroke="{INK}" stroke-width="1.8"/>')
    for bx in (128, 250, 378):
        a(arrow(bx, 252, bx, 244))
    a(glyph_menubar(250, 306))
    a(f'<line x1="250" y1="288" x2="250" y2="258" stroke="{INK}" stroke-width="1.8"/>')
    a('<text x="250" y="342" text-anchor="middle" class="cap">메뉴바 앱 (rumps · .dmg)</text>')

    # ══ 인제스트 ══════════════════════════════════════════════════
    a(arrow(462, 168, 598, 168))
    a('<text x="530" y="146" text-anchor="middle" class="edge mono">POST /api/ingest</text>')
    a('<text x="530" y="190" text-anchor="middle" class="cap2">벡터 + 리덕션 텍스트만</text>')
    a('<text x="530" y="205" text-anchor="middle" class="cap2">멱등 upsert · 원본은 안 나감</text>')

    # ══ ② EC2 — 서버 ══════════════════════════════════════════════
    a('<text x="790" y="44" text-anchor="middle" class="ttl">EC2 — 서버 (Ubuntu · Docker)</text>')
    a(box(600, 62, 380, 300))
    a(logo(icons, "amazonec2", 656, 62, 40))
    a('<text x="656" y="98" text-anchor="middle" class="cap">EC2</text>')

    a(logo(icons, "docker", 688, 172, 50, "Docker", "compose"))
    a(logo(icons, "fastapi", 878, 172, 48, "FastAPI", "uvicorn · SSE"))
    a(arrow(720, 172, 850, 172, "Run", 785, 162))

    a(glyph_chroma(700, 296))
    a('<text x="700" y="326" text-anchor="middle" class="cap">ChromaDB</text>')
    a('<text x="700" y="341" text-anchor="middle" class="cap2">events · 688MB</text>')

    a(logo(icons, "sqlite", 886, 296, 44, "sqlite ×3", "대화 · 요약 캐시"))

    a(arrow(846, 242, 742, 276, both=True))
    a(arrow(884, 244, 884, 272, both=True))

    # ══ ③ 사용자 ══════════════════════════════════════════════════
    a('<text x="1175" y="44" text-anchor="middle" class="ttl">사용자 — 브라우저</text>')
    a(box(1050, 62, 250, 300))
    a(logo(icons, "googlechrome", 1175, 140, 56, "Chrome", "정적 HTML 3장"))
    a(box(1076, 226, 198, 62, dash="5 4", stroke="#9CA3AF"))
    a('<text x="1175" y="252" text-anchor="middle" class="cap mono">/dashboard</text>')
    a('<text x="1175" y="272" text-anchor="middle" class="cap2">랜딩 · 물어보기 · 탐색</text>')
    a('<text x="1175" y="322" text-anchor="middle" class="cap2">첫 응답 1.4초</text>')
    a('<text x="1175" y="340" text-anchor="middle" class="cap2">근거는 200자 발췌만</text>')

    a(arrow(1048, 172, 982, 172, both=True))
    a('<text x="1015" y="162" text-anchor="middle" class="edge">SSE</text>')

    # ══ 외부 LLM (EC2의 오른쪽 아래 위성) ═════════════════════════
    a('<text x="1175" y="424" text-anchor="middle" class="ttl">LLM — 3중 스위치</text>')
    a(box(1050, 440, 250, 186, dash="6 5", stroke="#9CA3AF"))
    a(logo(icons, "ollama", 1100, 484, 34, "Ollama"))
    a('<text x="1100" y="531" text-anchor="middle" class="cap2">완전 로컬</text>')
    a(logo(icons, "openai", 1230, 484, 34, "OpenAI"))
    a('<text x="1230" y="531" text-anchor="middle" class="cap2">게이트웨이</text>')
    a(logo(icons, "anthropic", 1165, 570, 32, "Anthropic"))
    a('<text x="1165" y="617" text-anchor="middle" class="cap2">폴백</text>')
    a(arrow(1078, 438, 960, 366))
    a('<text x="1036" y="392" text-anchor="middle" class="edge">프롬프트</text>')

    # ══ 배포 — EC2 아래 위성 체인 ═════════════════════════════════
    a('<text x="440" y="556" text-anchor="middle" class="ttl">배포 — git push 한 번으로</text>')

    a(glyph_dev(140, 620))
    a('<text x="140" y="672" text-anchor="middle" class="cap">개발자</text>')
    a(logo(icons, "github", 350, 620, 50, "GitHub", "main 브랜치"))
    a(logo(icons, "githubactions", 560, 620, 48, "GitHub Actions", "deploy.yml"))
    a(logo(icons, "docker", 770, 620, 50, "GHCR", "이미지 빌드 · push"))

    a(arrow(180, 620, 318, 620, "git push", 249, 610))
    a(arrow(382, 620, 528, 620, "트리거", 455, 610))
    a(arrow(592, 620, 738, 620, "buildx", 665, 610))
    a(f'<line x1="802" y1="620" x2="928" y2="620" stroke="{INK}" stroke-width="1.8"/>')
    a(arrow(928, 620, 928, 368))
    a('<text x="966" y="486" text-anchor="middle" class="edge">ssh · pull</text>')
    a('<text x="970" y="502" text-anchor="middle" class="edge">up -d</text>')

    a('</svg>')
    return "\n".join(p) + "\n"


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "icons"
    ic = load_icons(d)
    print(f"아이콘 {len(ic)}개 로드")
    OUT.write_text(build(ic), encoding="utf-8")
    print(f"생성: {OUT}  ({OUT.stat().st_size:,}B)")
