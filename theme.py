"""Design tokens for the STARRYPAD panel.

Colour is a role, not a decoration. Pads are graphite; the accent marks what is
happening *now* (the pad being struck, the selected pad, an armed Record) and
nothing else. Anything that reaches for a raw RGB tuple instead of a token here
will drift the moment the palette moves.
"""

# --- surfaces -------------------------------------------------------------
GROUND = (20, 23, 24)        # window ground
PANEL = (29, 33, 34)         # sections, modals
PANEL_2 = (38, 43, 44)       # inset fields, waveform beds, active rows
PAD = (36, 42, 43)           # pad face at rest
PAD_HIT = (54, 62, 63)       # pad face while sounding
RULE = (49, 57, 58)          # borders
RULE_SOFT = (38, 45, 46)     # dividers inside a section

# --- ink ------------------------------------------------------------------
INK = (237, 239, 236)        # primary text
INK_2 = (145, 153, 149)      # labels, secondary text
INK_3 = (100, 107, 104)      # captions, disabled

# --- signal ---------------------------------------------------------------
ACCENT = (233, 162, 74)      # now: hit, selection, armed transport
ACCENT_SOFT = (58, 44, 24)   # accent wash behind a filled region
ON_ACCENT = (24, 19, 18)     # text on ACCENT or DANGER
SIGNAL = (69, 184, 166)      # connected, recorded, complete
SIGNAL_SOFT = (22, 48, 45)
DANGER = (217, 88, 78)       # Record, clipping, missing files

# --- scale ----------------------------------------------------------------
SPACE = (4, 8, 12, 16, 24, 32)
RADIUS = {"pad": 6, "panel": 7, "button": 5, "field": 5}

# Uppercase section labels are set in the condensed face with real tracking.
LABEL_TRACKING = 1.6


# The accent is the only colour the panel spends on "now", so it is the one
# worth letting people choose. Each entry rebuilds its own wash and ink.
ACCENT_CHOICES = (
    ("Amber", (233, 162, 74)),
    ("Ember", (232, 106, 74)),
    ("Rose", (232, 98, 143)),
    ("Violet", (169, 140, 240)),
    ("Cyan", (74, 192, 233)),
    ("Lime", (168, 209, 74)),
)
ACCENT_NAMES = tuple(name for name, _rgb in ACCENT_CHOICES)
DEFAULT_ACCENT = ACCENT_NAMES[0]


def relative_luminance(color):
    red, green, blue = (channel / 255.0 for channel in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def set_accent(name):
    """Point ACCENT, its wash and its ink at one of the named choices.

    Everything draws through `theme.ACCENT` rather than importing the value, so
    rebinding here reaches the next frame.
    """
    global ACCENT, ACCENT_SOFT, ON_ACCENT
    lookup = dict(ACCENT_CHOICES)
    ACCENT = lookup.get(name, lookup[DEFAULT_ACCENT])
    ACCENT_SOFT = mix(GROUND, ACCENT, 0.18)
    ON_ACCENT = (24, 19, 18) if relative_luminance(ACCENT) > 0.45 else (237, 239, 236)
    return ACCENT


def mix(base, other, amount):
    """Blend `other` into `base`; amount 0.0 keeps base, 1.0 returns other."""
    amount = max(0.0, min(1.0, float(amount)))
    return tuple(
        round(channel + (target - channel) * amount)
        for channel, target in zip(base, other)
    )


def hue_hint(color):
    """Damp a kit colour down to the 2px identity stripe on a pad."""
    return mix(color, PAD, 0.28)


def dim(color, amount=0.55):
    """Fade a colour toward the pad face, for muted pads and disabled controls."""
    return mix(color, PAD, amount)
