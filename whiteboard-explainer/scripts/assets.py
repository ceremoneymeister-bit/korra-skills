"""Нарезка листов иконок в отдельные PNG.

    python scripts/assets.py PROJECT

PROJECT/sheets.json: {"grid": [3, 2], "sheets": {"s1.png": ["robot", "person", …]},
"hand": "s5.png"} — листы лежат в PROJECT/sheets/, имена идут по ячейкам слева
направо, сверху вниз. Иконки → PROJECT/assets/icons/<имя>.png. Фон приводится
к чистому белому: движок рисования считает чернилами всё, что темнее 240.
Рука с маркером (необязательно) → PROJECT/assets/hand.png + hand.json.
"""
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import sys

PROJECT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
SHEETS = PROJECT / "sheets"
OUT = PROJECT / "assets"
CONFIG = json.loads((PROJECT / "sheets.json").read_text())
COLS, ROWS = CONFIG.get("grid", [3, 2])


def clean_white(rgb: np.ndarray) -> np.ndarray:
    out = rgb.copy()
    out[rgb.min(axis=2) > 238] = 255
    return out


def crop_cell(cell: np.ndarray, pad: int = 10) -> np.ndarray:
    ink = cell.min(axis=2) < 225
    # мелкий шум генератора не должен расширять рамку
    ink = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)) > 0
    ys, xs = np.where(ink)
    y1, y2 = max(ys.min() - pad, 0), min(ys.max() + pad, cell.shape[0])
    x1, x2 = max(xs.min() - pad, 0), min(xs.max() + pad, cell.shape[1])
    return cell[y1:y2, x1:x2]


def icons():
    (OUT / "icons").mkdir(parents=True, exist_ok=True)
    for sheet, names in CONFIG["sheets"].items():
        path = SHEETS / sheet
        if not path.exists():
            print("нет листа", path)
            continue
        rgb = np.array(Image.open(path).convert("RGB"))
        h, w = rgb.shape[:2]
        cw, ch = w // COLS, h // ROWS
        # Иконка — связная область чернил (с расширением, чтобы детали
        # склеились); к ячейке относится по центру. Так антенна робота,
        # залезшая в соседнюю ячейку, остаётся у своего робота.
        ink = (rgb.min(axis=2) < 225).astype(np.uint8)
        merged = cv2.dilate(ink, np.ones((25, 25), np.uint8))
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(merged)
        boxes = [[] for _ in names]
        for label in range(1, count):
            x, y, bw, bh, area = stats[label]
            if area < 400:
                continue
            cx, cy = centroids[label]
            cell = int(cy // ch) * COLS + int(cx // cw)
            if cell >= len(names):
                continue
            boxes[cell].append((x, y, x + bw, y + bh))
        for i, name in enumerate(names):
            if not boxes[i]:
                print("пустая ячейка", sheet, name)
                continue
            x1 = max(min(b[0] for b in boxes[i]) + 2, 0)
            y1 = max(min(b[1] for b in boxes[i]) + 2, 0)
            x2 = min(max(b[2] for b in boxes[i]) - 2, w)
            y2 = min(max(b[3] for b in boxes[i]) - 2, h)
            keep = np.isin(labels[y1:y2, x1:x2], [
                label for label in range(1, count)
                if stats[label][4] >= 400 and int(centroids[label][1] // ch) * COLS + int(centroids[label][0] // cw) == i
            ])
            icon = rgb[y1:y2, x1:x2].copy()
            icon[~keep] = 255
            icon = clean_white(icon)
            Image.fromarray(icon).save(OUT / "icons" / f"{name}.png")
            print(name, icon.shape[1], "x", icon.shape[0])


def hand():
    rgb = np.array(Image.open(SHEETS / CONFIG["hand"]).convert("RGB"))
    rgb = crop_cell(rgb, pad=4)
    h, w = rgb.shape[:2]
    white = (rgb.min(axis=2) > 232).astype(np.uint8)
    # фон — белое, связанное с краем листа; белое внутри руки остаётся
    mask = np.zeros((h + 2, w + 2), np.uint8)
    bg = white.copy()
    for seed in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if bg[seed[1], seed[0]]:
            cv2.floodFill(bg, mask, seed, 2)
    alpha = np.where(bg == 2, 0, 255).astype(np.uint8)
    alpha = cv2.GaussianBlur(alpha, (3, 3), 0)
    # кончик — самая нижне-левая точка непрозрачного
    ys, xs = np.where(alpha > 128)
    k = np.argmax(ys - xs)
    tip = (int(xs[k]), int(ys[k]))
    # кончик маркера перекрашиваем в цвет чернил: линии на доске чёрные
    lime = (rgb[:, :, 1] > 150) & (rgb[:, :, 2] < 120) & (rgb[:, :, 0] < 200)
    yy, xx = np.mgrid[0:h, 0:w]
    near_tip = (xx - tip[0]) ** 2 + (yy - tip[1]) ** 2 < (0.09 * w) ** 2
    rgb = rgb.copy()
    rgb[lime & near_tip] = (40, 40, 40)
    rgba = np.dstack([rgb, alpha])
    # Манжета плавно растворяется вправо: без ровного среза посреди кадра.
    ramp = np.clip((0.97 * w - np.arange(w)) / (0.17 * w), 0, 1)
    rgba[:, :, 3] = (rgba[:, :, 3] * ramp[None, :]).astype(np.uint8)
    Image.fromarray(rgba, "RGBA").save(OUT / "hand.png")
    (OUT / "hand.json").write_text(json.dumps({"w": w, "h": h, "tip": tip}))
    print("hand", w, "x", h, "tip", tip)


if __name__ == "__main__":
    icons()
    if CONFIG.get("hand"):
        hand()
