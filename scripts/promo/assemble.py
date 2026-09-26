"""Assemble only the recorded synthetic demo; requires local FFmpeg and Huihui WAVs."""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "test-results" / "promo-v0.1.1"
MEDIA = ROOT / "docs" / "media"
DURATIONS = [6.0, 6.0, 8.0, 4.5, 8.0]
NAMES = ["intro", "automatic", "manual", "export", "outro"]


def run(*args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], cwd=OUT, check=True
    )


def timestamp(t, ass=False):
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return (
        f"{int(h)}:{int(m):02}:{sec:05.2f}"
        if ass
        else f"{int(h):02}:{int(m):02}:{sec:06.3f}".replace(".", ",")
    )


def main():
    marks = json.loads((OUT / "marks.json").read_text(encoding="utf8"))
    narration = json.loads((MEDIA / "narration.json").read_text(encoding="utf8"))
    for name, duration in [("intro", 6), ("outro", 8)]:
        run(
            "-loop",
            "1",
            "-i",
            name + ".png",
            "-t",
            duration,
            "-vf",
            "fps=30,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-threads",
            "2",
            "-an",
            name + ".mp4",
        )

    def extract(start, end, duration, name):
        run(
            "-ss",
            start,
            "-t",
            end - start,
            "-i",
            "raw.webm",
            "-vf",
            f"setpts={duration / (end - start):.8f}*(PTS-STARTPTS),fps=30,format=yuv420p",
            "-t",
            duration,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-threads",
            "2",
            "-an",
            name + ".mp4",
        )

    extract(marks["import_start"], marks["import_end"], 2, "import")
    extract(marks["automatic_start"], marks["automatic_end"], 4, "masks")
    extract(marks["long_start"], marks["long_end"], 8, "manual")
    extract(marks["export_start"], marks["export_end"], 4.5, "export")
    (OUT / "concat.txt").write_text(
        "\n".join("file '" + x + ".mp4'" for x in ["intro", "import", "masks", "manual", "export", "outro"]),
        encoding="utf8",
    )
    run("-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "silent.mp4")

    srt, dialogues = [], []
    rate, width, channels = 22050, 2, 1
    elapsed = 0.0
    with wave.open(str(OUT / "narration.wav"), "wb") as output:
        output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        for i, (line, duration) in enumerate(zip(narration, DURATIONS)):
            with wave.open(str(OUT / (line["id"] + ".wav"))) as source:
                assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (
                    channels,
                    width,
                    rate,
                )
                data = source.readframes(source.getnframes())
            audio_duration = len(data) / (channels * width * rate)
            leading = 0.15
            assert audio_duration + leading < duration, f"Narration too long: {line['id']}"
            output.writeframes(b"\0" * (int(leading * rate) * width))
            output.writeframes(data)
            output.writeframes(
                b"\0" * ((round(duration * rate) - int(leading * rate) - len(data) // width) * width)
            )
            start, end = elapsed + leading, elapsed + duration - 0.08
            srt.append(f"{i + 1}\n{timestamp(start)} --> {timestamp(end)}\n{line['text']}\n")
            dialogues.append(
                f"Dialogue: 0,{timestamp(start, True)},{timestamp(end, True)},Caption,,0,0,0,,{line['text']}"
            )
            elapsed += duration

    (MEDIA / "yinqu-demo.zh-CN.srt").write_text("\n".join(srt), encoding="utf8")
    shutil.copyfile(MEDIA / "yinqu-demo.zh-CN.srt", OUT / "yinqu-demo.zh-CN.srt")
    ass = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Microsoft YaHei,35,&H00FFFFFF,&H00FFFFFF,&H00322B15,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,60,60,25,1
Style: Note,Microsoft YaHei,21,&H00B3D1C0,&H00B3D1C0,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,35,35,972,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ass += f"Dialogue: 0,0:00:00.00,{timestamp(elapsed, True)},Note,,0,0,0,,合成示例 · 识别等待已剪辑\n"
    ass += "\n".join(dialogues) + "\n"
    (OUT / "demo.ass").write_text(ass, encoding="utf8")
    run(
        "-i",
        "silent.mp4",
        "-i",
        "narration.wav",
        "-vf",
        "pad=1920:1080:0:0:color=0x15352b,ass=demo.ass",
        "-af",
        "loudnorm=I=-18:TP=-1.5:LRA=7",
        "-t",
        elapsed,
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "20",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "yinqu-demo-v0.1.1.mp4",
    )
    assert (OUT / "yinqu-demo-v0.1.1.mp4").stat().st_size < 20 * 1024**2
    print(json.dumps({"duration": elapsed, "bytes": (OUT / "yinqu-demo-v0.1.1.mp4").stat().st_size}))


if __name__ == "__main__":
    main()
