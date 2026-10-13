"""Листы кадров готового ролика: равномерно по времени, с метками секунд.

    python scripts/qa.py PROJECT/out/video.mp4 [кадров=32]
→ рядом с роликом qa-sheet-0.png, qa-sheet-1.png … (по 16 кадров 4×4)
Смотреть глазами: обрезанные буквы, пустые кадры, рука поверх подписи,
читаемость мелкого текста.
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

if len(sys.argv) < 2:
    sys.exit("укажи ролик: python scripts/qa.py PROJECT/out/video.mp4 [кадров]")
video = Path(sys.argv[1]).resolve()
count = int(sys.argv[2]) if len(sys.argv) > 2 else 32
qa = video.parent / "qa"
qa.mkdir(parents=True, exist_ok=True)
for old in qa.glob("*.png"):
    old.unlink()

duration = float(subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
    capture_output=True, text=True, check=True).stdout)
times = [round(duration * (i + 0.5) / count, 2) for i in range(count)]
frames = []
for t in times:
    path = qa / f"f_{t:06.2f}.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(t), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=640:-1", str(path)], check=True)
    frames.append((t, path))

for part in range(0, len(frames), 16):
    chunk = frames[part:part + 16]
    sheet = Image.new("RGB", (4 * 650, 4 * 370), (90, 90, 90))
    draw = ImageDraw.Draw(sheet)
    for i, (t, path) in enumerate(chunk):
        x, y = (i % 4) * 650, (i // 4) * 370
        sheet.paste(Image.open(path), (x, y))
        draw.rectangle([x, y, x + 64, y + 22], fill=(0, 0, 0))
        draw.text((x + 5, y + 5), f"{t:5.1f}", fill=(255, 255, 0))
    out = video.parent / f"qa-sheet-{part // 16}.png"
    sheet.save(out)
    print(out)
