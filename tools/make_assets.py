"""Generate the panel's two image assets.

Both are procedural, so they can be regenerated whenever the palette moves
instead of living as opaque binaries nobody can edit.

    python tools/make_assets.py
"""

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy  # noqa: E402
import pygame  # noqa: E402

import icons  # noqa: E402
import theme  # noqa: E402

GRAIN_SIZE = 256
ICON_SIZE = 1024


def make_grain(path):
    """A tileable monochrome noise tile, laid over flat fills at low opacity.

    Without it large areas of a single RGB value read as plastic; with it they
    read as a coated panel. Deterministic so the file never churns in git.
    """
    rng = numpy.random.default_rng(20260823)
    noise = rng.integers(0, 256, size=(GRAIN_SIZE, GRAIN_SIZE), dtype=numpy.uint8)

    # Blend the tile into itself, offset by half, so opposite edges match.
    half = GRAIN_SIZE // 2
    rolled = numpy.roll(numpy.roll(noise, half, axis=0), half, axis=1)
    ramp = numpy.linspace(0.0, 1.0, GRAIN_SIZE, dtype=numpy.float32)
    weight = numpy.minimum(ramp, ramp[::-1])[:, None] * numpy.minimum(ramp, ramp[::-1])[None, :]
    weight = (weight / weight.max()).astype(numpy.float32)
    blended = (noise * weight + rolled * (1.0 - weight)).astype(numpy.uint8)

    surface = pygame.Surface((GRAIN_SIZE, GRAIN_SIZE), pygame.SRCALPHA)
    rgb = pygame.surfarray.pixels3d(surface)
    alpha = pygame.surfarray.pixels_alpha(surface)
    rgb[:, :, 0] = rgb[:, :, 1] = rgb[:, :, 2] = 255
    alpha[:, :] = blended
    del rgb, alpha
    pygame.image.save(surface, str(path))
    return path


def make_icon(path, size=ICON_SIZE):
    """The app icon: four pads on a graphite tile, one of them lit."""
    surface = pygame.Surface((size, size), pygame.SRCALPHA)
    unit = size / 1024.0

    body = pygame.Rect(0, 0, size, size)
    pygame.draw.rect(surface, theme.GROUND, body, border_radius=round(224 * unit))
    inset = body.inflate(round(-24 * unit), round(-24 * unit))
    pygame.draw.rect(surface, theme.RULE, inset, width=max(1, round(6 * unit)),
                     border_radius=round(212 * unit))

    pad = round(300 * unit)
    gap = round(48 * unit)
    origin = (size - (pad * 2 + gap)) // 2
    for row in range(2):
        for column in range(2):
            rect = pygame.Rect(origin + column * (pad + gap), origin + row * (pad + gap), pad, pad)
            lit = row == 1 and column == 0
            pygame.draw.rect(surface, theme.PAD_HIT if lit else theme.PAD, rect,
                             border_radius=round(56 * unit))
            pygame.draw.rect(surface, theme.ACCENT if lit else theme.RULE, rect,
                             width=max(2, round(10 * unit)), border_radius=round(56 * unit))
            if lit:
                glow = rect.inflate(round(-90 * unit), round(-90 * unit))
                pygame.draw.rect(surface, theme.ACCENT, glow, border_radius=round(28 * unit))

    pygame.image.save(surface, str(path))
    return path


ICNS_SIZES = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2))
ICO_SIZES = (16, 32, 48, 64, 128, 256)


def make_icns(brand):
    """macOS bundle icon. Needs `iconutil`, so it is skipped off macOS."""
    if not shutil.which("iconutil"):
        return None
    iconset = brand / "starrypad.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    for edge, scale in ICNS_SIZES:
        suffix = "" if scale == 1 else "@2x"
        make_icon(iconset / f"icon_{edge}x{edge}{suffix}.png", edge * scale)
    target = brand / "starrypad.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)], check=True)
    shutil.rmtree(iconset)
    return target


def make_ico(brand):
    """Windows icon. An .ico is a directory of PNGs, so it needs no extra library."""
    payloads = []
    staging = brand / "_ico"
    staging.mkdir(exist_ok=True)
    for edge in ICO_SIZES:
        temp = staging / f"{edge}.png"
        make_icon(temp, edge)
        payloads.append((edge, temp.read_bytes()))
    shutil.rmtree(staging)

    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = len(header) + 16 * len(payloads)
    directory = b""
    body = b""
    for edge, data in payloads:
        directory += struct.pack(
            "<BBBBHHII", edge % 256, edge % 256, 0, 0, 1, 32, len(data), offset
        )
        body += data
        offset += len(data)
    target = brand / "starrypad.ico"
    target.write_bytes(header + directory + body)
    return target


def main():
    pygame.init()
    tex = ROOT / "assets" / "tex"
    brand = ROOT / "assets" / "brand"
    tex.mkdir(parents=True, exist_ok=True)
    brand.mkdir(parents=True, exist_ok=True)

    print("wrote", make_grain(tex / "grain-256.png").relative_to(ROOT))
    print("wrote", make_icon(brand / "icon-1024.png").relative_to(ROOT))
    for edge in (512, 256, 128, 64, 32, 16):
        print("wrote", make_icon(brand / f"icon-{edge}.png", edge).relative_to(ROOT))

    icns = make_icns(brand)
    if icns:
        print("wrote", icns.relative_to(ROOT))
    else:
        print("skipped starrypad.icns (iconutil is macOS only)")
    print("wrote", make_ico(brand).relative_to(ROOT))

    sheet_names = icons.names()
    print(f"{len(sheet_names)} vector icons are drawn in code, not stored as files")
    pygame.quit()


if __name__ == "__main__":
    main()
