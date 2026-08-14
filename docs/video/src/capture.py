#!/usr/bin/env python3
"""슬라이드/실제 UI를 프레임 단위로 캡처한다.

실시간 녹화가 아니라 **결정적 캡처**다. Web Animations API로 애니메이션 시각을
프레임마다 직접 찍어서(setTime) 스크린샷을 뜬다 — 프레임 드랍이 없고, 몇 번을
다시 돌려도 같은 결과가 나온다. 기능 화면(s22~s26)은 페이지의 진짜 타이핑
애니메이션과 SSE 렌더가 돌아야 하므로 가상 시계(page.clock)를 33ms씩 전진시킨다.

  ./capture.py slides      # s01~s26 (s22~s26 제외)
  ./capture.py features    # 실제 대시보드 UI 5종
  ./capture.py all
"""
import shutil
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
FRAMES = HERE / "frames"
FPS = 30
W, H = 1920, 1080

# ── 요소가 차례로 쌓여 들어오게 만드는 런타임 ────────────────────────
# 슬라이드 HTML에 --i를 손으로 박지 않는다. 문서 순서대로 자동 부여해야
# 슬라이드를 고쳐도 순서가 안 어긋난다.
RUNTIME = r"""
window.__prep = (sid) => {
  const slide = document.getElementById(sid);
  if (!slide) return "NO_SLIDE";

  // 스태거 대상 — 의미 단위 블록만. 이미 선택된 요소의 자손은 건너뛴다.
  const SEL = ".chapter,.wordmark,h1,h2,.lede,.sub,.rule,.card,.stat,.stage,.arr," +
              "tbody tr,table tr,.banner,.ba .box,.ba .mid-arr,.shot,.shot-cap," +
              ".mark,.kind,.cap,.flow";
  const picked = [];
  slide.querySelectorAll(SEL).forEach(el => {
    if (picked.some(p => p.contains(el))) return;
    picked.push(el);
  });
  picked.forEach((el, i) => {
    el.setAttribute("data-a", el.matches(".rule,.arr") ? "bar"
                            : el.matches(".shot,.mark") ? "fade" : "");
    el.style.setProperty("--i", i);
  });

  // 큰 수치는 0에서 올라간다 — 숫자가 그냥 나타나면 정보지만, 올라가면 성과가 된다
  window.__counts = [];
  slide.querySelectorAll(".stat .v").forEach(v => {
    const node = [...v.childNodes].find(n => n.nodeType === 3 && /\d/.test(n.textContent));
    if (!node) return;
    const m = node.textContent.match(/^(\D*?)([\d.]+)(.*)$/s);
    if (!m) return;
    const delay = (parseFloat(getComputedStyle(v.closest(".stat")).getPropertyValue("--i")) || 0) * 65;
    window.__counts.push({node, pre: m[1], target: parseFloat(m[2]),
                          post: m[3], decimals: (m[2].split(".")[1] || "").length, delay});
  });

  document.getAnimations().forEach(a => { a.pause(); a.currentTime = 0; });
  return picked.length;
};

window.__setTime = (t) => {
  document.getAnimations().forEach(a => { try { a.currentTime = t; } catch (e) {} });
  for (const c of window.__counts || []) {
    const p = Math.max(0, Math.min(1, (t - c.delay) / 1100));
    const eased = 1 - Math.pow(1 - p, 3);      // easeOutCubic — 끝에서 부드럽게 선다
    c.node.textContent = c.pre + (c.target * eased).toFixed(c.decimals) + c.post;
  }
};
"""


def _frames_for(seconds):
    return int(round(seconds * FPS))


def capture_slides(page, plan):
    for sid, seconds in plan:
        out = FRAMES / sid
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        page.goto(f"file://{HERE}/slides.html#{sid}")
        page.wait_for_timeout(220)
        page.evaluate(RUNTIME)
        n = page.evaluate("(s) => __prep(s)", sid)
        if n == "NO_SLIDE":
            raise SystemExit(f"슬라이드 없음: {sid}")

        total = _frames_for(seconds)
        for f in range(total):
            page.evaluate("(t) => __setTime(t)", f * 1000.0 / FPS)
            page.screenshot(path=str(out / f"{f:04d}.png"))
        print(f"  {sid}  {seconds:>4.1f}s  {total:>4}f")


# ── 실제 대시보드 UI ────────────────────────────────────────────────
def capture_features(page_factory, plan):
    """가상 시계를 33ms씩 밀면서 페이지의 진짜 렌더링을 그대로 찍는다."""
    for sid, turn, seconds in plan:
        out = FRAMES / sid
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        page = page_factory()
        page.clock.install()
        page.goto(f"file://{HERE}/harness.html?turn={turn}&live=1")
        page.wait_for_timeout(400)          # 최초 렌더(잔디/최근기록)는 실시간으로 끝낸다

        total = _frames_for(seconds)
        for f in range(total):
            page.clock.run_for(int(1000 / FPS))
            page.screenshot(path=str(out / f"{f:04d}.png"))
        page.close()
        print(f"  {sid}  turn={turn}  {seconds:>4.1f}s  {total:>4}f")


def capture_bands(page, features):
    """기능 화면에 얹을 자막 띠 — 배경 없이 알파 PNG로."""
    for sid, _turn, _sec in features:
        page.goto(f"file://{HERE}/slides.html#{sid}")
        page.wait_for_timeout(180)
        page.evaluate("document.documentElement.classList.add('bandonly')")
        page.evaluate(RUNTIME)
        page.evaluate("(s) => __prep(s)", sid)
        page.evaluate("__setTime(4000)")        # 등장 애니메이션이 끝난 상태로
        page.screenshot(path=str(FRAMES / f"{sid}band.png"), omit_background=True)
        page.evaluate("document.documentElement.classList.remove('bandonly')")
    print(f"  자막 띠 {len(features)}개")


def capture_demo_frame(page):
    """journey.gif를 얹을 배경 액자 한 장."""
    page.goto(f"file://{HERE}/slides.html#s21")
    page.wait_for_timeout(180)
    page.evaluate(RUNTIME)
    page.evaluate("__prep('s21')")
    page.evaluate("__setTime(4000)")
    page.screenshot(path=str(FRAMES / "s21.png"))


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    sys.path.insert(0, str(HERE))
    from compose import capture_plan            # 길이는 compose.py가 단일 출처

    slides, features = capture_plan()
    FRAMES.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", args=["--force-color-profile=srgb",
                                                            "--disable-lcd-text"])
        ctx = browser.new_context(viewport={"width": W, "height": H},
                                  device_scale_factor=1, color_scheme="dark")
        page = ctx.new_page()

        if what in ("all", "slides"):
            print(f"▸ 슬라이드 {len(slides)}장")
            capture_slides(page, slides)
            capture_demo_frame(page)
            capture_bands(page, features)
        if what in ("all", "features"):
            print(f"▸ 실제 UI {len(features)}종")
            capture_features(ctx.new_page, features)

        browser.close()


if __name__ == "__main__":
    main()
