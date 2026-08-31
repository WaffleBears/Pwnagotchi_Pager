import json
import logging
import os

from .. import config
from .pager import Pager


LOOK_R = '( o_o)'
LOOK_L = '(o_o )'
SLEEP = '(-_-)z'
AWAKE = '(O_O)'
BORED = '(-__-)'
INTENSE = '(0_0)'
COOL = '(B_B)'
HAPPY = '(^_^)'
EXCITED = '(*_*)'
MOTIVATED = '(>_<)'
DEMOTIVATED = '(=_=)'
LONELY = '(;_;)'
SAD = '(T_T)'
ANGRY = "(>_<')"
BROKEN = '(X_X)'

def all_faces():
    out = []
    for key, value in globals().items():
        if key.startswith('_') or not key.isupper():
            continue
        if isinstance(value, str):
            out.append(value)
    return out


def widest():
    faces = all_faces()
    return max(faces, key=len) if faces else '(o_o)'


CUSTOM_FILE = os.path.join(config.DATA_DIR, 'custom_themes.json')

DEFAULT = 'Abyss'


def rgb(r, g, b):
    return Pager.rgb(r, g, b)


def _mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _build(bg, surface, ink, accent, good, bad, warn, face=None):
    face = face or accent
    return {
        'bg': rgb(*bg),
        'panel': rgb(*surface),
        'chip': rgb(*_mix(surface, ink, 0.10)),
        'line': rgb(*_mix(surface, ink, 0.22)),
        'edge': rgb(*_mix(surface, accent, 0.55)),
        'text': rgb(*ink),
        'label': rgb(*_mix(bg, ink, 0.72)),
        'dim': rgb(*_mix(bg, ink, 0.60)),
        'title': rgb(*ink),
        'face': rgb(*face),
        'accent': rgb(*accent),
        'selected': rgb(*accent),
        'unselected': rgb(*_mix(bg, ink, 0.66)),
        'selbg': rgb(*accent),
        'seltext': rgb(*bg),
        'on': rgb(*good),
        'off': rgb(*bad),
        'warning': rgb(*warn),
    }


THEMES = {
    'Abyss': _build(
        bg=(9, 13, 22), surface=(19, 26, 42), ink=(226, 235, 248),
        accent=(88, 214, 255), good=(78, 222, 168), bad=(255, 106, 122),
        warn=(255, 190, 92)),
    'Ember': _build(
        bg=(20, 14, 11), surface=(38, 26, 20), ink=(246, 233, 220),
        accent=(255, 162, 66), good=(154, 214, 122), bad=(255, 104, 92),
        warn=(255, 214, 110)),
    'Orchid': _build(
        bg=(18, 12, 26), surface=(34, 22, 48), ink=(238, 228, 250),
        accent=(206, 130, 255), good=(120, 226, 190), bad=(255, 110, 150),
        warn=(255, 196, 120)),
    'Moss': _build(
        bg=(11, 18, 15), surface=(20, 32, 26), ink=(226, 240, 228),
        accent=(140, 226, 122), good=(120, 226, 160), bad=(240, 120, 108),
        warn=(240, 206, 110)),
    'Slate': _build(
        bg=(16, 17, 19), surface=(30, 32, 36), ink=(232, 234, 238),
        accent=(236, 90, 84), good=(126, 200, 150), bad=(236, 90, 84),
        warn=(238, 190, 100), face=(214, 220, 228)),
}

_KEYS = tuple(THEMES[DEFAULT].keys())


def _hex_to_color(value):
    text = str(value).strip().lstrip('#')
    if len(text) == 3:
        text = ''.join(c * 2 for c in text)
    if len(text) != 6 or any(c not in '0123456789abcdefABCDEF' for c in text):
        raise ValueError('bad colour %r' % value)
    return rgb(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def load_custom():
    if not os.path.exists(CUSTOM_FILE):
        return
    try:
        with open(CUSTOM_FILE) as f:
            custom = json.load(f)
    except Exception as e:
        logging.warning('could not read %s: %s', CUSTOM_FILE, e)
        return
    if not isinstance(custom, dict):
        return
    for name, colours in custom.items():
        if name in THEMES or not isinstance(colours, dict):
            continue
        try:
            base = dict(THEMES[DEFAULT])
            for key in _KEYS:
                if key in colours:
                    base[key] = _hex_to_color(colours[key])
            THEMES[name] = base
            if name not in config.THEME_NAMES:
                config.THEME_NAMES.append(name)
        except Exception as e:
            logging.warning('ignoring custom theme %r: %s', name, e)


def get(name):
    return THEMES.get(name, THEMES[DEFAULT])


load_custom()


MONO = os.path.join(config.FONTS_DIR, 'JetBrainsMono-Bold.ttf')
UI_BOLD = os.path.join(config.FONTS_DIR, 'Inter-SemiBold.ttf')
UI = UI_BOLD

FONT = UI

MICRO_SIZE = 14.0
SMALL_SIZE = 15.0
MEDIUM_SIZE = 17.0
LARGE_SIZE = 20.0

HEADER_H = 26
FOOTER_H = 26
COL_SPLIT = 206
PAD = 10
CHIP_H = 21
CHIP_PAD = 9
GUTTER = 12

FACE_MAX = 60.0
FACE_MIN = 26.0
FACE_STEP = 2.0


class Layout:
    def __init__(self, display):
        self.display = display
        self.width = display.width
        self.height = display.height

        self.header_h = HEADER_H
        self.footer_h = FOOTER_H
        self.header_y = 0
        self.body_top = HEADER_H + 1
        self.footer_y = self.height - FOOTER_H
        self.body_bottom = self.footer_y - 1
        self.split_x = COL_SPLIT

        self.left_w = self.split_x
        self.right_x = self.split_x + GUTTER
        self.right_w = self.width - self.right_x - PAD

        micro_h = display.ttf_height(UI, MICRO_SIZE) or int(MICRO_SIZE * 1.3)
        self.micro_h = micro_h
        self.small_h = display.ttf_height(UI, SMALL_SIZE) or int(SMALL_SIZE * 1.3)
        self.mono_h = display.ttf_height(MONO, SMALL_SIZE) or int(SMALL_SIZE * 1.3)
        self.bold_h = display.ttf_height(UI_BOLD, SMALL_SIZE) or int(SMALL_SIZE * 1.3)
        self.hint_h = micro_h
        self.hint_y = self.body_bottom - micro_h - 6

        self.status_y = self.body_top + 8
        self.status_line_h = (display.ttf_height(UI, MEDIUM_SIZE)
                              or int(MEDIUM_SIZE * 1.3)) + 4
        self.status_rows = max(1, (self.hint_y - 6 - self.status_y)
                               // self.status_line_h)

        self.chip_h = CHIP_H
        self.chip_y = self.body_bottom - CHIP_H - 8
        self.face_size = self._fit_face(display)
        self.face_h = display.ttf_height(MONO, self.face_size) or int(self.face_size * 1.2)
        face_band = self.chip_y - self.body_top
        self.face_y = self.body_top + max(0, (face_band - self.face_h) // 2)

        self.regions = {
            'name': (0, self.header_y, 236, self.header_h),
            'uptime': (236, self.header_y, 132, self.header_h),
            'battery': (368, self.header_y, self.width - 368, self.header_h),
            'face': (0, self.body_top, self.split_x, self.chip_y - self.body_top),
            'channel': (0, self.chip_y, self.split_x, CHIP_H + 8),
            'status': (self.split_x + 2, self.body_top,
                       self.width - self.split_x - 2,
                       self.hint_y - self.body_top),
            'hint': (self.split_x + 2, self.hint_y,
                     self.width - self.split_x - 2,
                     self.body_bottom - self.hint_y),
            'aps': (0, self.footer_y, 152, self.footer_h),
            'pwnd': (152, self.footer_y, 140, self.footer_h),
            'last': (292, self.footer_y, self.width - 292, self.footer_h),
        }

        self.bands = {
            'name': 'panel', 'uptime': 'panel', 'battery': 'panel',
            'aps': 'panel', 'pwnd': 'panel', 'last': 'panel',
        }

        logging.info('layout: face %.0fpt y%d h%d | status %dpx %drows | split x%d',
                     self.face_size, self.face_y, self.face_h,
                     self.right_w, self.status_rows, self.split_x)

    def _fit_face(self, display):
        budget_w = self.split_x - 2 * PAD
        budget_h = self.chip_y - self.body_top - 8
        face = widest()
        size = FACE_MAX
        while size > FACE_MIN:
            w = display.ttf_width(face, MONO, size) or 0
            h = display.ttf_height(MONO, size) or int(size * 1.2)
            if w <= budget_w and h <= budget_h:
                break
            size -= FACE_STEP
        return size


_wrap_cache = {}


MAX_WRAP_CHARS = 256
MAX_WRAP_LINES = 12


def wrap(display, text, max_px, font=UI, size=MEDIUM_SIZE):
    if not text or max_px <= 0:
        return [text] if text else []
    text = str(text)
    if len(text) > MAX_WRAP_CHARS:
        text = text[:MAX_WRAP_CHARS]
    key = (text, max_px, font, size)
    hit = _wrap_cache.get(key)
    if hit is not None:
        return hit
    if len(_wrap_cache) > 64:
        _wrap_cache.clear()

    def width(s):
        return display.ttf_width(s, font, size) or 0

    def longest_fit(word):
        low, high, best = 1, len(word) - 1, 1
        while low <= high:
            mid = (low + high) // 2
            if width(word[:mid] + '~') <= max_px:
                best, low = mid, mid + 1
            else:
                high = mid - 1
        return best

    lines = []
    current = ''
    for word in text.split(' '):
        if len(lines) >= MAX_WRAP_LINES:
            break
        while width(word) > max_px and len(word) > 1:
            if current:
                lines.append(current)
                current = ''
            if len(lines) >= MAX_WRAP_LINES:
                break
            cut = longest_fit(word)
            lines.append(word[:cut] + '~')
            word = word[cut:]
        if len(lines) >= MAX_WRAP_LINES:
            break
        if not current:
            current = word
        elif width(current + ' ' + word) <= max_px:
            current += ' ' + word
        else:
            lines.append(current)
            current = word
    if current and len(lines) < MAX_WRAP_LINES:
        lines.append(current)
    _wrap_cache[key] = lines
    return lines
