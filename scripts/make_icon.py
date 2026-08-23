#!/usr/bin/env python3
"""Vygeneruje ikonu aplikace pro macOS (.icns) bez externích nástrojů.

Kreslí se do numpy pole se čtyřnásobným převzorkováním kvůli hladkým hranám,
PNG se zapisuje přes zlib a kontejner .icns se skládá ručně — takže to jde
spustit kdekoli, ne jen na Macu s `iconutil`.
"""
from __future__ import annotations

import itertools
import struct
import zlib
from pathlib import Path

import numpy as np

# --- barvy (RGB 0-255) ---
BG_TOP = np.array([26, 34, 48], dtype=float)
BG_BOTTOM = np.array([9, 13, 20], dtype=float)
GREEN = np.array([46, 204, 143], dtype=float)
RED = np.array([242, 85, 90], dtype=float)
ACCENT = np.array([76, 154, 255], dtype=float)

SS = 4                      # převzorkování
ICNS_TYPES = [              # (typ, hrana v pixelech)
    (b"ic11", 32), (b"ic12", 64), (b"ic07", 128), (b"ic13", 256),
    (b"ic08", 256), (b"ic14", 512), (b"ic09", 512), (b"ic10", 1024),
]


def squircle_mask(size: int, inset: float = 0.045, power: float = 4.0) -> np.ndarray:
    """Maska zaobleného čtverce ve stylu macOS (superelipsa)."""
    axis = (np.arange(size) + 0.5) / size * 2.0 - 1.0
    x, y = np.meshgrid(axis, axis)
    radius = 1.0 - inset * 2
    value = (np.abs(x) / radius) ** power + (np.abs(y) / radius) ** power
    return (value <= 1.0).astype(float)


def vertical_gradient(size: int, top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    ramp = np.linspace(0.0, 1.0, size)[:, None, None]
    column = top[None, None, :] * (1 - ramp) + bottom[None, None, :] * ramp
    return np.repeat(column, size, axis=1)          # (size, size, 3)


def fill_rect(canvas: np.ndarray, x0: float, y0: float, x1: float, y1: float,
              color: np.ndarray, size: int) -> None:
    """Obdélník v relativních souřadnicích 0..1."""
    xs, xe = int(x0 * size), int(x1 * size)
    ys, ye = int(y0 * size), int(y1 * size)
    if xe <= xs:
        xe = xs + 1
    if ye <= ys:
        ye = ys + 1
    canvas[ys:ye, xs:xe] = color


def draw_line(canvas: np.ndarray, points: list[tuple[float, float]],
              color: np.ndarray, width: float, size: int, alpha: float = 1.0) -> None:
    """Lomená čára s měkkým okrajem (vzdálenost bodu od úsečky)."""
    axis = (np.arange(size) + 0.5) / size
    px, py = np.meshgrid(axis, axis)
    half = width / 2
    coverage = np.zeros((size, size))
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        dx, dy = x1 - x0, y1 - y0
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            continue
        t = np.clip(((px - x0) * dx + (py - y0) * dy) / length_sq, 0.0, 1.0)
        distance = np.hypot(px - (x0 + t * dx), py - (y0 + t * dy))
        edge = 1.0 / size
        coverage = np.maximum(coverage, np.clip((half - distance) / edge, 0.0, 1.0))
    coverage = (coverage * alpha)[:, :, None]
    canvas *= 1 - coverage
    canvas += color[None, None, :] * coverage


def render(size: int) -> np.ndarray:
    """Vrátí RGBA pole size×size×4 (uint8)."""
    work = size * SS
    canvas = vertical_gradient(work, BG_TOP, BG_BOTTOM)

    # jemné světlo shora, aby ikona nebyla placatá
    axis = (np.arange(work) + 0.5) / work
    gx, gy = np.meshgrid(axis, axis)
    glow = np.exp(-(((gx - 0.5) ** 2) / 0.18 + ((gy - 0.12) ** 2) / 0.05))[:, :, None]
    canvas += glow * 22

    # stoupající trendová linka pod svíčkami
    draw_line(canvas, [(0.20, 0.695), (0.38, 0.58), (0.60, 0.46), (0.795, 0.315)],
              ACCENT, width=0.028, size=work, alpha=0.32)

    # svíčky: (střed x, horní knot, tělo od, tělo do, dolní knot, rostoucí?)
    candles = [
        (0.215, 0.60, 0.64, 0.78, 0.83, False),
        (0.355, 0.46, 0.50, 0.68, 0.72, True),
        (0.500, 0.36, 0.42, 0.56, 0.62, True),
        (0.645, 0.30, 0.38, 0.50, 0.55, False),
        (0.790, 0.16, 0.22, 0.44, 0.50, True),
    ]
    body_half, wick_half = 0.042, 0.0075
    for cx, wick_top, body_top, body_bottom, wick_bottom, rising in candles:
        color = GREEN if rising else RED
        fill_rect(canvas, cx - wick_half, wick_top, cx + wick_half, wick_bottom, color, work)
        fill_rect(canvas, cx - body_half, body_top, cx + body_half, body_bottom, color, work)

    # oříznutí do tvaru ikony
    mask = squircle_mask(work)
    rgba = np.zeros((work, work, 4), dtype=float)
    rgba[:, :, :3] = np.clip(canvas, 0, 255)
    rgba[:, :, 3] = mask * 255

    # zmenšení průměrováním = vyhlazené hrany
    small = rgba.reshape(size, SS, size, SS, 4).mean(axis=(1, 3))
    return np.clip(small, 0, 255).astype(np.uint8)


def write_png(pixels: np.ndarray) -> bytes:
    """Minimální PNG kodér (RGBA, 8 bitů, filtr 0)."""
    height, width = pixels.shape[:2]
    raw = b"".join(b"\x00" + pixels[row].tobytes() for row in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build_icns(destination: Path) -> Path:
    """Složí .icns z PNG variant ve všech velikostech, které macOS používá."""
    cache: dict[int, bytes] = {}
    entries = []
    for icon_type, edge in ICNS_TYPES:
        if edge not in cache:
            cache[edge] = write_png(render(edge))
        payload = cache[edge]
        entries.append(icon_type + struct.pack(">I", len(payload) + 8) + payload)

    body = b"".join(entries)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
    return destination


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    icns = build_icns(root / "macos" / "AdaptiveTradingBot.app" / "Contents" / "Resources" / "AppIcon.icns")
    preview = root / "macos" / "icon-preview.png"
    preview.write_bytes(write_png(render(512)))
    print(f"ikona:  {icns}  ({icns.stat().st_size / 1024:.0f} kB)")
    print(f"náhled: {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
