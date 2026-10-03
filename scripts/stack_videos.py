"""Trim, crop and stack videos side by side or top to bottom into one web video.

Stacking several views into a single file keeps them frame-locked in the browser;
separate <video> elements drift apart. Each input can be trimmed and cropped,
then every panel is scaled to a common height (horizontal stack) or width
(vertical stack), joined, and encoded as H.264 MP4 and VP9 WebM, both in the
limited ("tv") colour range that browser decoders expect.

Options after an `-i FILE` apply to that input only (like ffmpeg, but after the
file rather than before it):

    python scripts/stack_videos.py --direction h --size 400 \\
        -i birdseye.mp4 \\
        -i panoramic_eye.mp4 --start 2 \\
        -i compound_eye.mp4 --start 2 --crop auto \\
        -o sim_hstack

    --start / --end   trim points in seconds; shift --start to line up clips
    --crop auto       remove black borders (ffmpeg cropdetect over the clip)
    --crop W:H:X:Y    explicit crop, in source pixels

Relative -i paths are read from local/ (the source clips are not published)
and a relative -o path is written to images/, whatever the current directory;
absolute paths are used as given, and --input-dir / --output-dir change the
defaults. The output length is the shortest trimmed input unless --duration is
given. Use --preview to write a single PNG of the layout, into the input
folder, without encoding. It prints the <source> tags to paste into the page,
with the right codecs strings.

Needs an ffmpeg with libx264 and libvpx-vp9. It is looked up via --ffmpeg, then
$FFMPEG, then PATH, then the imageio-ffmpeg package (`python -m pip install
imageio-ffmpeg`, a static build that includes both). conda-forge's Windows
ffmpeg lacks libvpx, so it can only write the MP4.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

REPO = Path(__file__).resolve().parent.parent
INPUT_DIR = REPO / "local"  # source clips (kept out of the published site)
OUTPUT_DIR = REPO / "images"  # stacked videos the pages play

# H.264 levels: (level, max macroblocks per frame, max macroblocks per second).
H264_LEVELS = [
    (30, 1620, 40500),
    (31, 3600, 108000),
    (32, 5120, 216000),
    (40, 8192, 245760),
    (42, 8704, 522240),
    (50, 22080, 589824),
    (51, 36864, 983040),
    (52, 36864, 2073600),
]
COLOR_TAGS = [
    "-color_range", "tv",
    "-colorspace", "bt709",
    "-color_primaries", "bt709",
    "-color_trc", "bt709",
]  # fmt: skip


@dataclass
class Clip:
    """One input video and how to cut it."""

    path: Path
    start: float = 0.0
    end: float | None = None
    crop: str | None = None  # "auto" or "W:H:X:Y"


@dataclass
class Probe:
    """Basic stream properties read from ffmpeg's banner."""

    width: int
    height: int
    fps: float
    duration: float


class InputAction(argparse.Action):
    """`-i FILE`: start a new clip that later per-input options modify."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        namespace.clips.append(Clip(Path(values)))


class ClipOptionAction(argparse.Action):
    """`--start/--end/--crop`: set a field on the most recent `-i` clip."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if not namespace.clips:
            parser.error(f"{option_string} must follow the -i it applies to")
        setattr(namespace.clips[-1], self.dest, values)


def find_ffmpeg(explicit: str | None) -> str:
    """Return a path to an ffmpeg executable, or exit with a hint."""
    for candidate in (explicit, os.environ.get("FFMPEG"), shutil.which("ffmpeg")):
        if candidate:
            return candidate
    try:
        import imageio_ffmpeg  # type: ignore  # untyped, and optional
    except ImportError:
        sys.exit(
            "ffmpeg not found. Pass --ffmpeg, set $FFMPEG, put it on PATH, "
            "or `python -m pip install imageio-ffmpeg`."
        )
    exe: str = imageio_ffmpeg.get_ffmpeg_exe()
    return exe


def run(cmd: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing text output."""
    return subprocess.run(
        list(cmd), capture_output=True, text=True, encoding="utf-8", check=check
    )


def has_encoder(ffmpeg: str, name: str) -> bool:
    """Whether this ffmpeg build includes the named encoder."""
    out = run([ffmpeg, "-hide_banner", "-encoders"]).stdout
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.M) is not None


def probe(ffmpeg: str, path: Path) -> Probe:
    """Read size, frame rate and duration of the first video stream."""
    if not path.is_file():
        sys.exit(f"No such file: {path}")
    banner = run([ffmpeg, "-hide_banner", "-i", str(path)], check=False).stderr
    video = re.search(r"Stream #\S+.*?Video:.*", banner)
    size = video and re.search(r", (\d{2,5})x(\d{2,5})", video.group(0))
    fps = video and re.search(r", ([\d.]+) (?:fps|tbr)", video.group(0))
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", banner)
    if not (size and fps and dur):
        sys.exit(f"Could not read video properties of {path}:\n{banner}")
    h, m, s = dur.groups()
    return Probe(
        width=int(size.group(1)),
        height=int(size.group(2)),
        fps=float(fps.group(1)),
        duration=int(h) * 3600 + int(m) * 60 + float(s),
    )


def detect_crop(ffmpeg: str, clip: Clip, end: float) -> str:
    """Find the box that holds all non-black content across the trimmed clip."""
    cmd = [ffmpeg, "-hide_banner", "-ss", str(clip.start), "-to", str(end)]
    cmd += ["-i", str(clip.path), "-vf", "cropdetect=round=2:reset=0", "-f", "null"]
    log = run([*cmd, "-"], check=False).stderr
    boxes = re.findall(r"crop=(\d+:\d+:\d+:\d+)", log)
    if not boxes:
        sys.exit(f"cropdetect found nothing in {clip.path}")
    return str(boxes[-1])  # reset=0 accumulates, so the last box covers all frames


def even(x: float) -> int:
    """Round to the nearest even integer (4:2:0 video needs even dimensions)."""
    return max(2, 2 * round(x / 2))


def h264_level(width: int, height: int, fps: float) -> int:
    """Smallest H.264 level whose frame-size and rate limits fit the output."""
    mbs = math.ceil(width / 16) * math.ceil(height / 16)
    for level, max_fs, max_mbps in H264_LEVELS:
        if mbs <= max_fs and mbs * fps <= max_mbps:
            return level
    sys.exit(f"{width}x{height} at {fps:g} fps is beyond H.264 level 5.2")


def build_graph(
    clips: list[Clip], crops: list[str | None], probes: list[Probe], args: Any
) -> tuple[str, int, int]:
    """Build the filter_complex string; return it with the output size."""
    horizontal = args.direction == "h"
    parts, labels = [], []
    out_w = out_h = 0
    for i, (crop, pr) in enumerate(zip(crops, probes)):
        w, h = pr.width, pr.height
        chain = [f"fps={args.fps}"]
        if crop:
            chain.append(f"crop={crop}")
            w, h = (int(v) for v in crop.split(":")[:2])
        if horizontal:
            w, h = even(w * args.size / h), args.size
        else:
            w, h = args.size, even(h * args.size / w)
        chain.append(f"scale={w}:{h}:out_range=tv,setsar=1")
        if args.gap and i < len(clips) - 1:
            pw, ph = (w + args.gap, h) if horizontal else (w, h + args.gap)
            chain.append(f"pad={pw}:{ph}:0:0:{args.gap_color}")
            w, h = pw, ph
        if horizontal:
            out_w, out_h = out_w + w, h
        else:
            out_w, out_h = w, out_h + h
        parts.append(f"[{i}:v]{','.join(chain)}[p{i}]")
        labels.append(f"[p{i}]")
    stack = "hstack" if horizontal else "vstack"
    parts.append(f"{''.join(labels)}{stack}=inputs={len(clips)},format=yuv420p[v]")
    return ";".join(parts), out_w, out_h


def input_args(clips: list[Clip], duration: float) -> list[str]:
    """`-ss/-t/-i` for each clip; seeking per input keeps the clips aligned."""
    out: list[str] = []
    for clip in clips:
        out += ["-ss", str(clip.start), "-t", f"{duration:.6f}", "-i", str(clip.path)]
    return out


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line; per-input options attach to the preceding -i."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.set_defaults(clips=[])
    clip = p.add_argument_group("per input (apply to the preceding -i)")
    clip.add_argument("-i", metavar="FILE", action=InputAction, help="input video")
    clip.add_argument("--start", type=float, action=ClipOptionAction, metavar="S")
    clip.add_argument("--end", type=float, action=ClipOptionAction, metavar="S")
    clip.add_argument("--crop", action=ClipOptionAction, metavar="auto|W:H:X:Y")
    p.add_argument(
        "-o",
        "--output",
        required=True,
        type=Path,
        help="output name without extension; .mp4/.webm are added",
    )
    p.add_argument(
        "--input-dir",
        type=Path,
        default=INPUT_DIR,
        help="folder for relative -i paths (default: local/)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="folder for a relative -o path (default: images/)",
    )
    p.add_argument(
        "--direction",
        choices=["h", "v"],
        default="h",
        help="h: side by side (default); v: top to bottom",
    )
    p.add_argument(
        "--size",
        type=int,
        default=400,
        help="panel height for h, width for v, in pixels (default 400)",
    )
    p.add_argument("--gap", type=int, default=0, help="pixels between panels")
    p.add_argument("--gap-color", default="black", help="ffmpeg colour for gaps")
    p.add_argument(
        "--duration",
        type=float,
        help="output length in s (default: shortest trimmed input)",
    )
    p.add_argument("--fps", type=float, help="output frame rate (default: first input)")
    p.add_argument(
        "--formats",
        default="mp4,webm",
        help="comma-separated subset of mp4,webm (default both)",
    )
    p.add_argument(
        "--crf-mp4", type=int, default=23, help="x264 quality (lower=better)"
    )
    p.add_argument(
        "--crf-webm", type=int, default=30, help="VP9 quality (lower=better)"
    )
    p.add_argument(
        "--preview",
        type=float,
        nargs="?",
        const=-1.0,
        metavar="S",
        help="only write OUTPUT_preview.png at S s (default: midpoint)",
    )
    p.add_argument("--ffmpeg", help="ffmpeg executable to use")
    args = p.parse_args(argv)
    if not args.clips:
        p.error("give at least one -i FILE")
    args.formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    if bad := set(args.formats) - {"mp4", "webm"}:
        p.error(f"unknown format(s): {', '.join(sorted(bad))}")
    # Joining an absolute path onto a folder yields the absolute path unchanged.
    for c in args.clips:
        c.path = args.input_dir / c.path
    args.output = args.output_dir / args.output
    return args


def web_path(path: Path) -> str:
    """Path as a page at the repo root would reference it."""
    try:
        return path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        return path.name


def main(argv: Sequence[str] | None = None) -> None:
    """Probe, crop-detect, then encode the stacked video (or a preview frame)."""
    args = parse_args(argv)
    ffmpeg = find_ffmpeg(args.ffmpeg)
    clips: list[Clip] = args.clips
    probes = [probe(ffmpeg, c.path) for c in clips]

    ends = [c.end if c.end is not None else pr.duration for c, pr in zip(clips, probes)]
    lengths = [end - c.start for c, end in zip(clips, ends)]
    for c, length in zip(clips, lengths):
        if length <= 0:
            sys.exit(f"{c.path}: --start is at or after the end of the clip")
    duration = args.duration or min(lengths)
    if args.duration and args.duration > min(lengths) + 1e-3:
        sys.exit(f"--duration {args.duration} is longer than the shortest clip")
    if args.duration is None and max(lengths) - min(lengths) > 0.05:
        print(
            f"note: trimmed lengths differ ({', '.join(f'{x:g}' for x in lengths)}"
            f" s); cutting all to {duration:g} s"
        )
    args.fps = args.fps or probes[0].fps

    crops: list[str | None] = []
    for c in clips:
        if c.crop == "auto":
            crops.append(detect_crop(ffmpeg, c, c.start + duration))
            print(f"{c.path.name}: auto crop {crops[-1]}")
        elif c.crop and not re.fullmatch(r"\d+:\d+:\d+:\d+", c.crop):
            sys.exit(f"{c.path}: --crop must be 'auto' or W:H:X:Y, not {c.crop!r}")
        else:
            crops.append(c.crop)

    graph, out_w, out_h = build_graph(clips, crops, probes, args)
    base = [ffmpeg, "-hide_banner", "-v", "error", "-y", *input_args(clips, duration)]
    base += ["-filter_complex", graph, "-map", "[v]", "-an"]
    args.output.parent.mkdir(parents=True, exist_ok=True)

    if args.preview is not None:
        t = duration / 2 if args.preview < 0 else args.preview
        png = args.input_dir / (args.output.name + "_preview.png")
        run([*base, "-ss", str(t), "-frames:v", "1", str(png)])
        print(f"wrote {png} ({out_w}x{out_h}, frame at {t:g} s)")
        return

    sources = []
    if "webm" in args.formats:
        if not has_encoder(ffmpeg, "libvpx-vp9"):
            sys.exit(
                f"{ffmpeg} has no libvpx-vp9; use --formats mp4 or another "
                "ffmpeg (e.g. `python -m pip install imageio-ffmpeg`)"
            )
        webm = args.output.with_name(args.output.name + ".webm")
        run(
            [
                *base,
                "-c:v",
                "libvpx-vp9",
                "-b:v",
                "0",
                "-crf",
                str(args.crf_webm),
                "-row-mt",
                "1",
                *COLOR_TAGS,
                str(webm),
            ]
        )
        print(f"wrote {webm} ({out_w}x{out_h}, {duration:g} s)")
        sources.append((webm, 'video/webm; codecs="vp9"'))
    if "mp4" in args.formats:
        level = h264_level(out_w, out_h, args.fps)
        mp4 = args.output.with_name(args.output.name + ".mp4")
        run(
            [
                *base,
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                str(args.crf_mp4),
                "-profile:v",
                "high",
                "-level",
                f"{level / 10:.1f}",
                *COLOR_TAGS,
                "-movflags",
                "+faststart",
                str(mp4),
            ]
        )
        print(f"wrote {mp4} ({out_w}x{out_h}, {duration:g} s)")
        sources.append((mp4, f'video/mp4; codecs="avc1.6400{level:02X}"'))

    print("\n<source> tags (WebM first; paths relative to the repo root):")
    for path, mime in sources:
        print(f"  <source src=\"{web_path(path)}\" type='{mime}'>")


if __name__ == "__main__":
    main()
