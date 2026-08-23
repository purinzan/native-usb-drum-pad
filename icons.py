"""Icons drawn with primitives instead of bitmaps.

The panel renders to a 1040x820 surface that is then scaled to 1.0, 1.25 or 1.5.
A PNG sprite authored for one of those factors softens at the other two, so every
glyph here is built from lines and polygons on a 16x16 grid and stays sharp at
any size. Each painter receives the target rect and the ink colour.
"""

import math

import pygame

GRID = 16.0


def _project(rect, points):
    """Map 16x16 grid coordinates onto the target rect."""
    scale_x = rect.width / GRID
    scale_y = rect.height / GRID
    return [(rect.x + x * scale_x, rect.y + y * scale_y) for x, y in points]


def _stroke(surface, rect, color, points, width=2, closed=False):
    pygame.draw.lines(surface, color, closed, _project(rect, points), width)


def _fill(surface, rect, color, points):
    pygame.draw.polygon(surface, color, _project(rect, points))


def _play(surface, rect, color):
    _fill(surface, rect, color, [(4, 3), (13, 8), (4, 13)])


def _stop(surface, rect, color):
    _fill(surface, rect, color, [(4, 4), (12, 4), (12, 12), (4, 12)])


def _record(surface, rect, color):
    pygame.draw.circle(surface, color, rect.center, max(3, round(rect.width * 0.3)))


def _pause(surface, rect, color):
    _fill(surface, rect, color, [(5, 4), (7, 4), (7, 12), (5, 12)])
    _fill(surface, rect, color, [(9, 4), (11, 4), (11, 12), (9, 12)])


def _overdub(surface, rect, color):
    pygame.draw.circle(surface, color, rect.center, max(3, round(rect.width * 0.32)), 2)
    _stroke(surface, rect, color, [(8, 5.5), (8, 10.5)])
    _stroke(surface, rect, color, [(5.5, 8), (10.5, 8)])


def _undo(surface, rect, color):
    _stroke(surface, rect, color, [(4, 7.5), (11.5, 7.5), (11.5, 12.5)])
    _stroke(surface, rect, color, [(7, 4.5), (4, 7.5), (7, 10.5)])


def _redo(surface, rect, color):
    _stroke(surface, rect, color, [(12, 7.5), (4.5, 7.5), (4.5, 12.5)])
    _stroke(surface, rect, color, [(9, 4.5), (12, 7.5), (9, 10.5)])


def _gear(surface, rect, color):
    centre = pygame.Vector2(rect.center)
    outer = rect.width * 0.46
    inner = rect.width * 0.30
    teeth = []
    for step in range(12):
        angle = math.radians(step * 30.0)
        radius = outer if step % 2 == 0 else inner
        teeth.append((centre.x + math.cos(angle) * radius, centre.y + math.sin(angle) * radius))
    pygame.draw.polygon(surface, color, teeth, 2)
    pygame.draw.circle(surface, color, rect.center, max(2, round(rect.width * 0.15)), 2)


def _metronome(surface, rect, color):
    _stroke(surface, rect, color, [(5, 13), (7.5, 3), (9.5, 3), (12, 13)], closed=True)
    _stroke(surface, rect, color, [(8.5, 12), (10.5, 5.5)])


def _repeat(surface, rect, color):
    _stroke(surface, rect, color, [(4, 6.5), (11.5, 6.5), (11.5, 9)])
    _stroke(surface, rect, color, [(9.5, 4.5), (11.5, 6.5), (9.5, 8.5)])
    _stroke(surface, rect, color, [(12, 10.5), (4.5, 10.5), (4.5, 8)])
    _stroke(surface, rect, color, [(6.5, 12.5), (4.5, 10.5), (6.5, 8.5)])


def _magnet(surface, rect, color):
    _stroke(surface, rect, color, [(4, 13), (4, 8), (5, 5.5), (8, 4.5), (11, 5.5), (12, 8), (12, 13)])
    _stroke(surface, rect, color, [(4, 10.5), (7, 10.5)])
    _stroke(surface, rect, color, [(9, 10.5), (12, 10.5)])


def _zap(surface, rect, color):
    _fill(surface, rect, color, [(9.5, 2), (4.5, 9), (7.5, 9), (6.5, 14), (11.5, 7), (8.5, 7)])


def _microphone(surface, rect, color):
    pygame.draw.rect(
        surface, color,
        pygame.Rect(rect.x + rect.width * 0.38, rect.y + rect.height * 0.16,
                    rect.width * 0.24, rect.height * 0.42),
        border_radius=max(2, round(rect.width * 0.12)),
    )
    _stroke(surface, rect, color, [(4.5, 8), (4.5, 9.5)])
    _stroke(surface, rect, color, [(11.5, 8), (11.5, 9.5)])
    _stroke(surface, rect, color, [(4.5, 9.5), (8, 11.5), (11.5, 9.5)])
    _stroke(surface, rect, color, [(8, 11.5), (8, 13.5)])


def _waveform(surface, rect, color):
    for x, height in ((3.5, 3), (6, 7), (8.5, 10), (11, 6), (13, 3.5)):
        _stroke(surface, rect, color, [(x, 8 - height / 2), (x, 8 + height / 2)])


def _scissors(surface, rect, color):
    _stroke(surface, rect, color, [(4.5, 3.5), (11, 10.5)])
    _stroke(surface, rect, color, [(11.5, 3.5), (5, 10.5)])
    pygame.draw.circle(surface, color, _project(rect, [(4.5, 12)])[0], max(2, round(rect.width * 0.11)), 2)
    pygame.draw.circle(surface, color, _project(rect, [(11.5, 12)])[0], max(2, round(rect.width * 0.11)), 2)


def _folder(surface, rect, color):
    _stroke(surface, rect, color, [(3, 12.5), (3, 4), (6.5, 4), (8, 6), (13, 6), (13, 12.5)], closed=True)


def _share(surface, rect, color):
    _stroke(surface, rect, color, [(8, 11), (8, 3.5)])
    _stroke(surface, rect, color, [(5, 6.5), (8, 3.5), (11, 6.5)])
    _stroke(surface, rect, color, [(4, 9.5), (4, 13), (12, 13), (12, 9.5)])


def _grid(surface, rect, color):
    for row in (4.5, 9):
        for column in (4, 8.5):
            pygame.draw.rect(
                surface, color,
                pygame.Rect(*_project(rect, [(column, row)])[0],
                            rect.width * 0.22, rect.height * 0.22),
                border_radius=1,
            )


def _layers(surface, rect, color):
    _stroke(surface, rect, color, [(3, 6), (8, 3.5), (13, 6), (8, 8.5)], closed=True)
    _stroke(surface, rect, color, [(3, 9.5), (8, 12), (13, 9.5)])


def _sliders(surface, rect, color):
    for y, knob in ((5, 6), (8, 10), (11, 7.5)):
        _stroke(surface, rect, color, [(3, y), (13, y)])
        pygame.draw.circle(surface, color, _project(rect, [(knob, y)])[0], max(2, round(rect.width * 0.12)))


def _link(surface, rect, color):
    _stroke(surface, rect, color, [(6.5, 5), (5, 5), (3.5, 6.5), (3.5, 9.5), (5, 11), (6.5, 11)])
    _stroke(surface, rect, color, [(9.5, 5), (11, 5), (12.5, 6.5), (12.5, 9.5), (11, 11), (9.5, 11)])
    _stroke(surface, rect, color, [(6, 8), (10, 8)])


def _close(surface, rect, color):
    _stroke(surface, rect, color, [(5, 5), (11, 11)])
    _stroke(surface, rect, color, [(11, 5), (5, 11)])


def _chevron_left(surface, rect, color):
    _stroke(surface, rect, color, [(10, 4), (6, 8), (10, 12)])


def _chevron_right(surface, rect, color):
    _stroke(surface, rect, color, [(6, 4), (10, 8), (6, 12)])


PAINTERS = {
    "play": _play,
    "stop": _stop,
    "record": _record,
    "pause": _pause,
    "overdub": _overdub,
    "undo": _undo,
    "redo": _redo,
    "gear": _gear,
    "metronome": _metronome,
    "repeat": _repeat,
    "magnet": _magnet,
    "zap": _zap,
    "microphone": _microphone,
    "waveform": _waveform,
    "scissors": _scissors,
    "folder": _folder,
    "share": _share,
    "grid": _grid,
    "layers": _layers,
    "sliders": _sliders,
    "link": _link,
    "close": _close,
    "chevron_left": _chevron_left,
    "chevron_right": _chevron_right,
}


def draw(surface, name, rect, color):
    """Paint `name` inside `rect`. Unknown names draw nothing rather than raise."""
    painter = PAINTERS.get(name)
    if painter is None:
        return
    painter(surface, pygame.Rect(rect), color)


def names():
    return tuple(sorted(PAINTERS))
