"""Bundled typography for the panel.

Fonts ship with the app rather than coming from `SysFont`, for three reasons:
Windows and macOS otherwise render the UI in two different typefaces, the system
UI faces have proportional digits so every BPM or millisecond readout jitters as
it updates, and neither one covers the Japanese characters that CoreMIDI puts in
port names such as "SINCO ポート3".

Barlow and Barlow Condensed are one superfamily, so labels and body text share
skeletons. IBM Plex Mono carries every number in the UI.
"""

import functools
from pathlib import Path

import pygame

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

LABEL = FONT_DIR / "BarlowCondensed-SemiBold.ttf"
UI = FONT_DIR / "Barlow-Medium.ttf"
HEAD = FONT_DIR / "Barlow-SemiBold.ttf"
DATA = FONT_DIR / "IBMPlexMono-Medium.ttf"

# Latin faces cover neither kana nor kanji, so non-Latin text falls back to
# whichever CJK face the host provides. Order runs macOS, then Windows.
_CJK_CANDIDATES = (
    "hiraginosans",
    "hiraginosansgb",
    "hiraginokakugothicpron",
    "yugothic",
    "yugothicui",
    "meiryo",
    "msgothic",
    "notosansjp",
    "notosanscjkjp",
    "applesdgothicneo",
    "arialunicodems",
)


@functools.lru_cache(maxsize=1)
def _cjk_path():
    for name in _CJK_CANDIDATES:
        path = pygame.font.match_font(name)
        if path:
            return path
    return None


def _needs_fallback(text):
    return any(ord(character) > 0x2FF for character in text)


class Face:
    """A bundled font that quietly hands non-Latin text to a system face.

    Exposes `render` so it drops in wherever a `pygame.font.Font` was used.
    """

    def __init__(self, path, size, tracking=0.0, upper=False):
        self.font = pygame.font.Font(str(path), size)
        self.size = size
        self.tracking = tracking
        self.upper = upper
        self._fallback = None

    @property
    def fallback(self):
        if self._fallback is None:
            path = _cjk_path()
            self._fallback = pygame.font.Font(path, self.size) if path else self.font
        return self._fallback

    def get_height(self):
        return self.font.get_height()

    def measure(self, text):
        if self.upper:
            text = text.upper()
        font = self.fallback if _needs_fallback(text) else self.font
        width, height = font.size(text)
        if self.tracking and len(text) > 1:
            width += round(self.tracking * (len(text) - 1))
        return width, height

    def render(self, text, antialias=True, color=(255, 255, 255), background=None):
        text = str(text)
        if self.upper:
            text = text.upper()
        font = self.fallback if _needs_fallback(text) else self.font
        if not self.tracking or len(text) < 2:
            return font.render(text, antialias, color, background)
        return self._render_tracked(font, text, antialias, color, background)

    def _render_tracked(self, font, text, antialias, color, background):
        """SDL_ttf has no letter-spacing, so tracked labels are laid out by hand."""
        glyphs = [font.render(character, antialias, color) for character in text]
        step = round(self.tracking)
        width = sum(glyph.get_width() for glyph in glyphs) + step * (len(glyphs) - 1)
        height = font.get_height()
        surface = pygame.Surface((max(1, width), height), pygame.SRCALPHA)
        if background is not None:
            surface.fill(background)
        x = 0
        for glyph in glyphs:
            surface.blit(glyph, (x, 0))
            x += glyph.get_width() + step
        return surface


def build():
    """Every face the panel draws with, created once after `pygame.font.init()`."""
    return {
        "label": Face(LABEL, 15, tracking=1.6, upper=True),   # section labels
        "pad": Face(LABEL, 14, tracking=0.8, upper=True),     # pad faces, narrower
        "ui": Face(UI, 18),                                   # buttons, body
        "small": Face(UI, 16),                                # secondary text
        "head": Face(HEAD, 25),                               # panel headings
        "big": Face(HEAD, 42),                                # modal titles
        "data": Face(DATA, 15),                               # inline numbers
        "data_md": Face(DATA, 23),                            # header readouts
        "data_lg": Face(DATA, 28),                            # BPM, velocity
        "data_sm": Face(DATA, 12),                            # note numbers, ms
    }
