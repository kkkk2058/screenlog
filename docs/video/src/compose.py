#!/usr/bin/env python3
"""프레임 시퀀스 → 클립 → 최종 MP4.

길이는 전부 여기 두 테이블에만 있다. capture.py도 이 테이블을 읽어서 필요한
만큼만 찍는다 — 길이를 고칠 곳이 두 군데가 되면 반드시 어긋난다.
"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).parent
FRAMES, CLIPS, OUT = HERE / "frames", HERE / "clips", HERE / "out"
FPS = 30
XF = 0.45                      # 크로스페이드

DEMO = ("demo", 11.0)          # journey.gif — 실제 최초 사용 흐름

# 실제 대시보드 UI (harness.html의 turn 번호)
FEATURES = {"s22": 0, "s23": 1, "s24": 2, "s25": 3, "s26": 4}

FULL = [
    ("s01", 4.5), ("s02", 4.0), ("s03", 6.5),
    ("s04", 8.5), ("s05", 9.0),
    ("s06", 3.5), DEMO,
    ("s07", 8.5),
    ("s22", 8.0), ("s23", 7.5), ("s24", 7.5), ("s25", 7.5), ("s26", 8.5),
    ("s08", 6.5),
    ("s09", 9.0), ("s10", 9.0),
    ("s11", 5.5), ("s12", 8.5), ("s13", 9.0), ("s14", 9.0),
    ("s15", 9.0), ("s16", 8.5),
    ("s17", 8.5), ("s18", 8.0), ("s19", 7.0),
]

RECAP = [
    ("s01", 4.0), ("s03", 5.5), ("s20", 7.0),
    ("s06", 3.0), ("demo", 10.0), ("s07", 7.0),
    ("s22", 6.5), ("s23", 6.0), ("s24", 6.0), ("s25", 6.0), ("s26", 6.5),
    ("s09", 7.0), ("s16", 7.5), ("s17", 6.0), ("s19", 6.0),
]


def capture_plan():
    """(슬라이드, 기능UI) — 각각 두 영상에서 쓰이는 최대 길이만큼만 찍는다."""
    longest = {}
    for sid, sec in FULL + RECAP:
        longest[sid] = max(longest.get(sid, 0), sec)
    longest.pop("demo", None)

    slides = sorted((s, round(sec + 0.2, 2)) for s, sec in longest.items() if s not in FEATURES)
    feats = sorted((s, FEATURES[s], round(sec + 0.2, 2)) for s, sec in longest.items() if s in FEATURES)
    return slides, feats


def _run(args):
    subprocess.run(args, check=True)


def render_clips():
    """프레임 시퀀스를 세그먼트 클립으로 굽는다. 기능 UI에는 설명 띠를 얹는다."""
    CLIPS.mkdir(exist_ok=True)
    slides, feats = capture_plan()

    for sid, _ in slides:
        src = FRAMES / sid
        if not src.exists():
            raise SystemExit(f"프레임 없음: {src} — capture.py를 먼저 돌려라")
        _run(["ffmpeg", "-y", "-loglevel", "error",
              "-framerate", str(FPS), "-i", str(src / "%04d.png"),
              "-c:v", "libx264", "-crf", "14", "-preset", "fast",
              "-pix_fmt", "yuv420p", str(CLIPS / f"{sid}.mp4")])

    for sid, _turn, _ in feats:
        src, band = FRAMES / sid, FRAMES / f"{sid}band.png"
        if not src.exists():
            raise SystemExit(f"프레임 없음: {src}")
        # 실제 UI 위에 설명 띠를 0.9초 뒤부터 부드럽게 얹는다.
        # 화면이 먼저 움직이고 자막이 따라붙어야 "설명"이 아니라 "장면"이 된다.
        _run(["ffmpeg", "-y", "-loglevel", "error",
              "-framerate", str(FPS), "-i", str(src / "%04d.png"),
              "-loop", "1", "-i", str(band),
              "-filter_complex",
              "[1:v]format=rgba,fade=t=in:st=0.9:d=0.7:alpha=1[b];"
              "[0:v][b]overlay=0:0:shortest=1,format=yuv420p[v]",
              "-map", "[v]", "-c:v", "libx264", "-crf", "14", "-preset", "fast",
              str(CLIPS / f"{sid}.mp4")])
    print(f"  클립 {len(slides) + len(feats)}개")


def build_demo():
    """journey.gif를 액자에 넣는다. 원본이 880x550이라 살짝만 키우고 샤픈한다."""
    CLIPS.mkdir(exist_ok=True)
    _run(["ffmpeg", "-y", "-loglevel", "error",
          "-loop", "1", "-i", str(FRAMES / "s21.png"),
          "-ignore_loop", "0", "-i", str(HERE / "../../images/journey.gif"),
          "-filter_complex",
          "[0:v]scale=1920:1080,setsar=1[bg];"
          "[1:v]scale=1376:860:flags=lanczos,unsharp=5:5:0.7:5:5:0.0,setsar=1[g];"
          f"[bg][g]overlay=272:168:shortest=0,fps={FPS},format=yuv420p[v]",
          "-map", "[v]", "-t", "11.2", "-c:v", "libx264", "-crf", "14",
          "-preset", "fast", str(CLIPS / "demo.mp4")])


def build(seq, out):
    total = sum(d for _, d in seq) - (len(seq) - 1) * XF

    args = ["ffmpeg", "-y", "-loglevel", "error", "-stats"]
    for sid, _ in seq:
        args += ["-i", str(CLIPS / f"{sid}.mp4")]
    args += ["-f", "lavfi", "-t", f"{total}", "-i", "anullsrc=r=48000:cl=stereo"]

    parts = []
    for i, (_, dur) in enumerate(seq):
        parts.append(f"[{i}:v]trim=duration={dur},setpts=PTS-STARTPTS,fps={FPS},"
                     f"format=yuv420p[v{i}]")

    prev, running = "v0", seq[0][1]
    for i in range(1, len(seq)):
        parts.append(f"[{prev}][v{i}]xfade=transition=fade:duration={XF}:"
                     f"offset={running - XF:.3f}[x{i}]")
        prev = f"x{i}"
        running += seq[i][1] - XF

    # 필름 그레인 아주 약간 — 어두운 그라데이션의 밴딩을 덮는다.
    # 세게 넣으면 압축이 무너져 3분짜리가 60MB를 넘는다(실측). 2면 밴딩은
    # 가려지고 용량은 절반 이하로 떨어진다.
    parts.append(f"[{prev}]noise=alls=2:allf=t+u,"
                 f"fade=t=in:st=0:d=0.9,fade=t=out:st={total - 1.3:.3f}:d=1.3[vout]")

    args += ["-filter_complex", ";".join(parts),
             "-map", "[vout]", "-map", f"{len(seq)}:a",
             "-c:v", "libx264", "-preset", "slow", "-crf", "21",
             "-pix_fmt", "yuv420p", "-r", str(FPS),
             "-c:a", "aac", "-b:a", "96k", "-shortest",
             "-movflags", "+faststart", str(out)]
    print(f"\n▶ {out.name}  —  {len(seq)}컷 · {total:.1f}초 "
          f"({int(total // 60)}:{int(total % 60):02d})")
    _run(args)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "clips"):
        build_demo()
        render_clips()
    if what in ("all", "full"):
        build(FULL, OUT / "screenlog-full.mp4")
    if what in ("all", "recap"):
        build(RECAP, OUT / "screenlog-recap.mp4")
