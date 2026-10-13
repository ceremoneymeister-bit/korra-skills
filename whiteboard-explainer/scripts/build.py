"""Рисованная объяснялка: доска PROJECT/board.html → MP4 1920×1080.

    python scripts/build.py PROJECT check     # кадр и снимки элементов, «ошибок: 0»
    python scripts/build.py PROJECT preview   # доска картинками: PROJECT/out/preview-*.png
    python scripts/build.py PROJECT render    # ролик PROJECT/out/<output из #timeline>

Каждый элемент доски снимается отдельно (Playwright) и получает карту
времени проявления пикселей: иконки и текст — движком whiteboard-animator
(MIT, PyPI 0.1.1), пути SVG — по длине пути. Кодер проявляет пиксели по
времени, ведёт камеру по доске, рисует руку с маркером у кончика линии
и накрывает доску вуалью перед финальной карточкой.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.spatial import cKDTree

SKILL_DIR = Path(__file__).resolve().parent.parent
PROJECT = Path.cwd()          # папка с board.html; задаётся первым аргументом
OUT = PROJECT / "out"
OUT_W, OUT_H = 1920, 1080
FADE = 0.08          # пиксель проявляется за столько секунд
INK = 250            # всё темнее — часть рисунка (с полутонами краёв)
HAND_BOARD_H = 390   # высота руки на доске, px (на экране — с масштабом камеры)


# ───────────────────────── снимки элементов ─────────────────────────

COLLECT_JS = r"""
() => {
  const board = document.getElementById('board').getBoundingClientRect();
  return [...document.querySelectorAll('.el')].map(el => {
    const d = el.dataset;
    const item = {id: el.id, mode: d.mode || 'engine', layer: +(d.layer || 0),
                  d: +(d.d || 1), gap: d.gap === undefined ? 0.1 : +d.gap};
    if (item.mode === 'path') {
      let x1 = 1e9, y1 = 1e9, x2 = -1e9, y2 = -1e9, samples = [];
      for (const p of el.querySelectorAll('path')) {
        const sw = parseFloat(getComputedStyle(p).strokeWidth) || 10;
        const b = p.getBBox();
        x1 = Math.min(x1, b.x - sw); y1 = Math.min(y1, b.y - sw);
        x2 = Math.max(x2, b.x + b.width + sw); y2 = Math.max(y2, b.y + b.height + sw);
        const L = p.getTotalLength(), n = Math.max(2, Math.ceil(L / 3));
        for (let i = 0; i <= n; i++) {
          const q = p.getPointAtLength(L * i / n);
          samples.push([q.x, q.y]);
        }
      }
      item.box = [x1, y1, x2, y2];
      item.samples = samples;
    } else {
      // Рукописные буквы выходят за рамку блока (хвост последней «Т»,
      // «у», «р», «й»): снимаем с запасом в долю кегля, потом режем по чернилам.
      const r = el.getBoundingClientRect();
      const fs = parseFloat(getComputedStyle(el).fontSize) || 0;
      const pad = el.tagName === 'IMG' ? 8 : Math.max(24, Math.round(fs * 0.6));
      item.box = [r.left - board.left - pad, r.top - board.top - pad,
                  r.right - board.left + pad, r.bottom - board.top + pad];
    }
    item.box = item.box.map(Math.round);
    return item;
  });
}
"""


CANVAS = (5760, 3240)   # уточняется из #timeline в open_board


def open_board(pw):
    global CANVAS
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 5760, "height": 3240}, device_scale_factor=1)
    page.goto((PROJECT / "board.html").as_uri())
    CANVAS = tuple(json.loads(page.inner_text("#timeline"))["canvas"])
    page.set_viewport_size({"width": CANVAS[0], "height": CANVAS[1]})
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(400)
    return browser, page


def timeline_spec(page) -> dict:
    return json.loads(page.inner_text("#timeline"))


def shoot(page, items: list[dict]) -> dict[str, np.ndarray]:
    """Каждый элемент отдельно: остальные скрыты."""
    page.add_style_tag(content=".el { visibility: hidden !important; } .el.solo { visibility: visible !important; }")
    crops = {}
    for item in items:
        x1, y1, x2, y2 = item["box"]
        x1, y1 = max(x1, 0), max(y1, 0)
        x2, y2 = min(x2, CANVAS[0]), min(y2, CANVAS[1])
        item["box"] = [x1, y1, x2, y2]
        page.evaluate("id => document.getElementById(id).classList.add('solo')", item["id"])
        png = page.screenshot(clip={"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1})
        page.evaluate("id => document.getElementById(id).classList.remove('solo')", item["id"])
        rgb = np.array(Image.open(__import__("io").BytesIO(png)).convert("RGB"))
        ink = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) < INK
        if not ink.any():
            raise RuntimeError(f"{item['id']}: на снимке нет чернил")
        h, w = ink.shape
        touches = ink[:, 0].any() or ink[:, -1].any() or ink[0, :].any() or ink[-1, :].any()
        inner = (x1 > 0, x2 < CANVAS[0], y1 > 0, y2 < CANVAS[1])
        if touches and all(inner):
            raise RuntimeError(f"{item['id']}: чернила касаются края снимка — мал запас")
        # Рамка элемента — по настоящим чернилам (+3 px), она же для проверки кадра.
        ys, xs = np.where(ink)
        cy1, cy2 = max(ys.min() - 3, 0), min(ys.max() + 4, h)
        cx1, cx2 = max(xs.min() - 3, 0), min(xs.max() + 4, w)
        crops[item["id"]] = rgb[cy1:cy2, cx1:cx2]
        item["box"] = [x1 + cx1, y1 + cy1, x1 + cx2, y1 + cy2]
    return crops


# ───────────────────────── карты времени ─────────────────────────

def engine_timing(rgb: np.ndarray, duration: float) -> np.ndarray:
    """Время проявления каждого пикселя (0..duration), inf — фон."""
    from whiteboard_animator.animator import WhiteboardAnimator
    anim = WhiteboardAnimator(fade_duration=FADE)
    img = anim._enforce_white_bg(rgb)
    small = anim._downscale(img)
    comps = anim._find_components(small)
    h, w = small.shape[:2]
    timing = np.full((h, w), np.inf, dtype=np.float32)
    if comps:
        comps = anim._sort_components_layered(comps)
        for comp, start, dur in anim._schedule_components(comps, duration):
            ct = anim._trace_component(comp, start, dur)
            sl = (slice(comp["top"], comp["bottom"] + 1), slice(comp["left"], comp["right"] + 1))
            m = comp["mask"][sl]
            timing[sl][m] = ct[sl][m]
    if small.shape[:2] != rgb.shape[:2]:
        timing = cv2.resize(timing, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_NEAREST)
    finite = np.isfinite(timing)
    if finite.any():
        top = float(timing[finite].max())
        if top > 0:
            timing[finite] *= duration / top
    return timing


def path_timing(rgb: np.ndarray, origin: tuple[int, int], samples: list, duration: float) -> np.ndarray:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ys, xs = np.where(gray < INK)
    timing = np.full(gray.shape, np.inf, dtype=np.float32)
    if len(ys):
        pts = np.stack([xs + origin[0], ys + origin[1]], axis=1)
        _, idx = cKDTree(np.asarray(samples)).query(pts)
        timing[ys, xs] = idx / max(len(samples) - 1, 1) * duration
    return timing


def complete(timing: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    """Полутона краёв без своего времени берут время ближайшего пикселя."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    need = (gray < INK) & ~np.isfinite(timing)
    finite = np.isfinite(timing)
    if need.any() and finite.any():
        _, (iy, ix) = ndimage.distance_transform_edt(~finite, return_indices=True)
        timing[need] = timing[iy[need], ix[need]]
    timing[gray >= INK] = np.inf
    return timing


# ───────────────────────── расписание ─────────────────────────

def schedule(items: list[dict], spec: dict) -> float:
    t = 0.0
    for item in items:
        t += item["gap"]
        item["start"] = round(t, 3)
        t += item["d"]
        item["end"] = round(t, 3)
    return t + spec.get("end_hold", 2.0)


def ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u) if u < 1 else 1.0


def smoother(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * u * (u * (u * 6 - 15) + 10)


class Camera:
    """Окно камеры на доске: центр x, y и ширина w (высота — 16:9)."""

    DRIFT = 0.02   # медленный наезд на паузах, доля ширины
    DRIFT_T = 9.0

    def __init__(self, spec: dict, by_id: dict, total: float):
        shots = spec["camera"]
        first = shots[0]
        self.moves = []   # (t0, t1, target)
        for shot in shots[1:]:
            t1 = by_id[shot["at"]]["start"] - shot.get("lead", 0.6)
            self.moves.append((t1, t1 + shot.get("move", 1.2), (shot["x"], shot["y"], shot["w"])))
        self.start = (first["x"], first["y"], first["w"])

    def at(self, t: float) -> tuple[float, float, float]:
        cur = self.start
        hold_from = 0.0
        for t0, t1, target in self.moves:
            if t < t0:
                break
            base = self._drift(cur, t0 - hold_from)
            if t < t1:
                u = smoother((t - t0) / (t1 - t0))
                return self._mix(base, target, u)
            cur, hold_from = target, t1
        return self._drift(cur, t - hold_from)

    def _drift(self, win, held: float):
        k = 1 - self.DRIFT * (1 - math.exp(-max(held, 0) / self.DRIFT_T))
        return (win[0], win[1], win[2] * k)

    @staticmethod
    def _mix(a, b, u):
        """Переезд дугой: на длинном пути камера слегка отъезжает, чтобы
        между участками доски в кадре оставался контекст, а не пустое поле."""
        travel = math.hypot(b[0] - a[0], b[1] - a[1])
        bump = min(travel / (1.6 * max(a[2], b[2])), 0.45) * 4 * u * (1 - u)
        w = math.exp(math.log(a[2]) + (math.log(b[2]) - math.log(a[2])) * u) * (1 + bump)
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, w)


# ───────────────────────── кодер ─────────────────────────

class Layer:
    def __init__(self, size):
        self.buf = np.full((size[1], size[0], 3), 255, dtype=np.uint8)
        self.t = np.empty(0, np.float32)
        self.y = np.empty(0, np.int32)
        self.x = np.empty(0, np.int32)
        self.c = np.empty((0, 3), np.uint8)
        self.done = 0

    def add(self, t, y, x, c):
        self.t = np.concatenate([self.t, t]); self.y = np.concatenate([self.y, y])
        self.x = np.concatenate([self.x, x]); self.c = np.concatenate([self.c, c])

    def seal(self):
        order = np.argsort(self.t, kind="stable")
        self.t, self.y, self.x, self.c = self.t[order], self.y[order], self.x[order], self.c[order]

    def step(self, now: float):
        """Зафиксировать проявленное; вернуть полосу проявляющихся пикселей."""
        cut = int(np.searchsorted(self.t, now - FADE, side="right"))
        if cut > self.done:
            sl = slice(self.done, cut)
            self.buf[self.y[sl], self.x[sl]] = self.c[sl]
            self.done = cut
        end = int(np.searchsorted(self.t, now, side="right"))
        return slice(self.done, end)

    def tip(self, band: slice):
        if band.stop - band.start <= 0:
            return None
        sl = slice(max(band.start, band.stop - 80), band.stop)
        return float(self.x[sl].mean()), float(self.y[sl].mean())


def view(buf: np.ndarray, win, pyramid_cache: dict) -> np.ndarray:
    """Окно доски → кадр 1920×1080; при сильном отдалении — с уменьшенной копии."""
    cx, cy, w = win
    h = w * OUT_H / OUT_W
    s = OUT_W / w
    src, k = buf, 1
    if s < 0.7:
        k = 2 if s >= 0.36 else 4
        key = (id(buf), k)
        if key not in pyramid_cache:
            pyramid_cache[key] = cv2.resize(buf, (buf.shape[1] // k, buf.shape[0] // k), interpolation=cv2.INTER_AREA)
        src = pyramid_cache[key]
    sk = s * k
    m = np.array([[sk, 0, -s * (cx - w / 2)], [0, sk, -s * (cy - h / 2)]], dtype=np.float32)
    return cv2.warpAffine(src, m, (OUT_W, OUT_H), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def to_screen(p, win):
    cx, cy, w = win
    h = w * OUT_H / OUT_W
    s = OUT_W / w
    return (p[0] - (cx - w / 2)) * s, (p[1] - (cy - h / 2)) * s


def to_board(p, win):
    cx, cy, w = win
    h = w * OUT_H / OUT_W
    s = OUT_W / w
    return p[0] / s + (cx - w / 2), p[1] / s + (cy - h / 2)


class Hand:
    """Рука с маркером живёт на доске: двигается вместе с ней при переездах
    камеры и масштабируется с приближением. Без рисования уходит за кадр."""

    REST = (OUT_W + 300.0, OUT_H + 160.0)   # точка за кадром, экранные px

    def __init__(self):
        base = PROJECT / "assets" if (PROJECT / "assets" / "hand.png").is_file() else SKILL_DIR / "assets"
        meta = json.loads((base / "hand.json").read_text())
        self.src = Image.open(base / "hand.png").convert("RGBA")
        self.src_tip = meta["tip"]
        self.cache = {}
        self.pos = None      # кончик на доске; None — за кадром
        self.idle = 0.0

    def update(self, target, dt, win):
        rest = to_board(self.REST, win)
        if self.pos is None:
            self.pos = list(rest)
        if target is None:
            self.idle += dt
            goal, tau = (rest, 0.3) if self.idle > 0.3 else (self.pos, 1.0)
        else:
            self.idle = 0.0
            far = math.dist(to_screen(self.pos, win), to_screen(target, win)) > 450
            goal, tau = target, (0.09 if far else 0.045)
        a = 1 - math.exp(-dt / tau)
        self.pos = [self.pos[0] + (goal[0] - self.pos[0]) * a, self.pos[1] + (goal[1] - self.pos[1]) * a]

    def sprite(self, height: int):
        height -= height % 4          # меньше разных размеров при наезде камеры
        if height not in self.cache:
            if len(self.cache) > 12:
                self.cache.pop(next(iter(self.cache)))
            k = height / self.src.height
            img = self.src.resize((max(round(self.src.width * k), 1), height), Image.LANCZOS)
            self.cache[height] = (np.array(img).astype(np.float32), (self.src_tip[0] * k, self.src_tip[1] * k))
        return self.cache[height]

    def draw(self, frame: np.ndarray, win):
        height = max(int(round(HAND_BOARD_H * OUT_W / win[2])), 24)
        rgba, tip = self.sprite(height)
        sx, sy = to_screen(self.pos, win)
        x0, y0 = int(round(sx - tip[0])), int(round(sy - tip[1]))
        hh, ww = rgba.shape[:2]
        fx1, fy1 = max(x0, 0), max(y0, 0)
        fx2, fy2 = min(x0 + ww, OUT_W), min(y0 + hh, OUT_H)
        if fx2 <= fx1 or fy2 <= fy1:
            return
        patch = rgba[fy1 - y0:fy2 - y0, fx1 - x0:fx2 - x0]
        a = patch[:, :, 3:4] / 255.0
        region = frame[fy1:fy2, fx1:fx2].astype(np.float32)
        frame[fy1:fy2, fx1:fx2] = (region * (1 - a) + patch[:, :, :3] * a).astype(np.uint8)


def render(items, crops, spec, total, out_path: Path):
    canvas = tuple(spec["canvas"])
    fps = spec.get("fps", 30)
    layers = [Layer(canvas), Layer(canvas)]
    t0 = time.monotonic()
    for item in items:
        rgb = crops[item["id"]]
        x1, y1 = item["box"][0], item["box"][1]
        if item["mode"] == "path":
            timing = path_timing(rgb, (x1, y1), item["samples"], item["d"])
        else:
            timing = engine_timing(rgb, item["d"])
        timing = complete(timing, rgb)
        ys, xs = np.where(np.isfinite(timing))
        layers[item["layer"]].add((timing[ys, xs] + item["start"]).astype(np.float32),
                                  (ys + y1).astype(np.int32), (xs + x1).astype(np.int32), rgb[ys, xs])
        print(f"  {item['id']:<15} {item['start']:6.2f}–{item['end']:6.2f}  {len(ys):>8} px", flush=True)
    for layer in layers:
        layer.seal()
    print(f"карты времени: {time.monotonic() - t0:.1f} с")

    by_id = {item["id"]: item for item in items}
    cam = Camera(spec, by_id, total)
    full = card_window(spec)   # финальная карточка — неподвижный общий план
    layer1_first = min((it["start"] for it in items if it["layer"] == 1), default=total)
    veil = spec.get("veil") or {"lead": 0.8, "dur": 0.6, "alpha": 0.95}
    veil_t = (by_id[veil["at"]]["start"] if "at" in veil else layer1_first) - veil.get("lead", 0.8)
    hand = Hand()

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{OUT_W}x{OUT_H}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    frames = int(math.ceil(total * fps))
    t1 = time.monotonic()
    for i in range(frames):
        now = i / fps
        win = cam.at(now)
        cache = {}
        frame = None
        tip = None
        for li, layer in enumerate(layers):
            band = layer.step(now)
            n = band.stop - band.start
            saved = None
            if n > 0:
                ys, xs = layer.y[band], layer.x[band]
                saved = layer.buf[ys, xs].copy()
                a = np.sqrt(np.clip((now - layer.t[band]) / FADE, 0, 1))[:, None]
                layer.buf[ys, xs] = (saved + (layer.c[band].astype(np.float32) - saved) * a).astype(np.uint8)
            if li == 0:
                frame = view(layer.buf, win, cache)
                tip = layer.tip(band)
                if now > veil_t:
                    va = veil["alpha"] * ease((now - veil_t) / veil["dur"])
                    frame = (frame.astype(np.float32) * (1 - va) + 255 * va).astype(np.uint8)
            elif now >= layer1_first - 0.01:
                top = view(layer.buf, full, cache)
                frame = (frame.astype(np.uint16) * top.astype(np.uint16) // 255).astype(np.uint8)
                p = layer.tip(band)
                if p is not None:
                    tip = p
            if saved is not None:
                layer.buf[layer.y[band], layer.x[band]] = saved
        hand_win = full if now >= layer1_first - 0.01 else win
        hand.update(tip, 1 / fps, hand_win)
        hand.draw(frame, hand_win)
        proc.stdin.write(frame.tobytes())
        if i % (fps * 5) == 0:
            print(f"  кадр {i}/{frames} ({now:5.1f} с)", flush=True)
    proc.stdin.close()
    proc.wait()
    print(f"кодирование: {time.monotonic() - t1:.1f} с → {out_path}")


# ───────────────────────── команды ─────────────────────────

def card_window(spec: dict) -> tuple:
    if "card_window" in spec:
        return tuple(float(v) for v in spec["card_window"])
    w, h = spec["canvas"]
    return (w / 2, h / 2, float(w))


def main():
    global PROJECT, OUT
    from playwright.sync_api import sync_playwright
    args = sys.argv[1:]
    if args and args[0] not in ("check", "preview", "render"):
        PROJECT = Path(args.pop(0)).resolve()
        OUT = PROJECT / "out"
    cmd = args[0] if args else "render"
    if not (PROJECT / "board.html").is_file():
        sys.exit(f"нет {PROJECT / 'board.html'}: укажи папку проекта первым аргументом")
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser, page = open_board(pw)
        spec = timeline_spec(page)
        items = page.evaluate(COLLECT_JS)
        total = schedule(items, spec)
        if cmd == "check":
            shoot(page, items)
            # 1) Каждый элемент, пока рисуется, целиком в кадре (поле 1,5 %).
            # 2) Рисовать можно, когда камера прошла ≥ 70 % переезда.
            # 3) Уже нарисованное не режется краем кадра на стоянках камеры.
            by_id = {it["id"]: it for it in items}
            cam = Camera(spec, by_id, total)
            full = card_window(spec)

            def window(t, layer=0):
                cx, cy, w = full if layer == 1 else cam.at(float(t))
                h = w * OUT_H / OUT_W
                return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, w

            bad = 0
            for it in items:
                for t in np.linspace(it["start"], it["end"], 6):
                    x1w, y1w, x2w, y2w, w = window(t, it["layer"])
                    m = 0.015 * w
                    x1, y1, x2, y2 = it["box"]
                    if x1 < x1w + m or x2 > x2w - m or y1 < y1w + m or y2 > y2w - m:
                        print(f"  ВНЕ КАДРА {it['id']} t={t:.2f} box={it['box']}")
                        bad += 1
                        break
                for t0, t1, _ in cam.moves:
                    if it["layer"] == 0 and t0 < it["end"] and t1 > it["start"] and it["start"] < t0 + 0.7 * (t1 - t0):
                        print(f"  КАМЕРА ЕЩЁ ЕДЕТ: {it['id']} начинается на {it['start']:.2f}, переезд {t0:.2f}–{t1:.2f}")
                        bad += 1
            cuts = {}
            stops = [0.0] + [t1 for _, t1, _ in cam.moves]
            for t in stops:
                x1w, y1w, x2w, y2w, _ = window(t + 0.05)
                for it in items:
                    if it["layer"] or it["end"] > t + 0.05:
                        continue
                    x1, y1, x2, y2 = it["box"]
                    inter = x1 < x2w and x2 > x1w and y1 < y2w and y2 > y1w
                    inside = x1 >= x1w and x2 <= x2w and y1 >= y1w and y2 <= y2w
                    if inter and not inside:
                        cuts.setdefault(f"{t:6.2f}", []).append(it["id"])
            for t, ids in cuts.items():
                print(f"  обрезано на стоянке {t}: {', '.join(ids)}")
            print(f"длительность {total:.1f} с; ошибок: {bad}")
            return
        if cmd == "preview":
            # Доска (слой 0) — областями из "previews" или целиком; карточка (слой 1) — её окном.
            regions = spec.get("previews") or [{"name": "board", "x": 0, "y": 0, "w": CANVAS[0], "h": CANVAS[1]}]
            has_card = any(it["layer"] == 1 for it in items)
            for layer in ((0, 1) if has_card else (0,)):
                page.evaluate("""l => document.querySelectorAll('.el').forEach(e =>
                    e.style.visibility = (+(e.dataset.layer || 0) === l) ? 'visible' : 'hidden')""", layer)
                if layer == 0:
                    for r in regions:
                        page.screenshot(path=str(OUT / f"preview-{r['name']}.png"),
                                        clip={"x": r.get("x", 0), "y": r.get("y", 0), "width": r["w"], "height": r["h"]})
                else:
                    cx, cy, w = card_window(spec)
                    h = w * OUT_H / OUT_W
                    page.screenshot(path=str(OUT / "preview-card.png"),
                                    clip={"x": cx - w / 2, "y": cy - h / 2, "width": w, "height": h})
            print(f"длительность {total:.1f} с, элементов {len(items)}")
            for it in items:
                print(f"  {it['id']:<15} {it['start']:6.2f}–{it['end']:6.2f}  {it['box']}")
            return
        crops = shoot(page, items)
        browser.close()
    print(f"длительность {total:.1f} с, элементов {len(items)}")
    render(items, crops, spec, total, OUT / spec.get("output", "video.mp4"))


if __name__ == "__main__":
    main()
