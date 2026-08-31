
import os
from ctypes import CDLL, Structure, c_int, c_uint8, c_uint16, c_uint32, c_float, c_char, c_char_p, c_void_p, POINTER, byref


class PagerInput(Structure):
    _fields_ = [
        ("current", c_uint8),
        ("pressed", c_uint8),
        ("released", c_uint8),
    ]


class PagerInputEvent(Structure):
    _fields_ = [
        ("button", c_uint8),
        ("type", c_int),
        ("timestamp", c_uint32),
    ]


PAGER_EVENT_NONE = 0
PAGER_EVENT_PRESS = 1
PAGER_EVENT_RELEASE = 2

_lib_paths = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "libpagerctl.so"),
    "./libpagerctl.so",
]

_lib = None
for path in _lib_paths:
    if os.path.exists(path):
        _lib = CDLL(path)
        break

if _lib is None:
    raise OSError("Could not find libpagerctl.so - build with: make remote-build")


class Pager:

    BLACK = 0x0000
    WHITE = 0xFFFF
    RED = 0xF800
    GREEN = 0x07E0
    BLUE = 0x001F
    YELLOW = 0xFFE0
    CYAN = 0x07FF
    MAGENTA = 0xF81F
    ORANGE = 0xFD20
    PURPLE = 0x8010
    GRAY = 0x8410

    ROTATION_0 = 0
    ROTATION_90 = 90
    ROTATION_180 = 180
    ROTATION_270 = 270

    FONT_SMALL = 1
    FONT_MEDIUM = 2
    FONT_LARGE = 3

    BTN_UP = 0x01
    BTN_DOWN = 0x02
    BTN_LEFT = 0x04
    BTN_RIGHT = 0x08
    BTN_A = 0x10
    BTN_B = 0x20
    BTN_POWER = 0x40

    EVENT_NONE = 0
    EVENT_PRESS = 1
    EVENT_RELEASE = 2

    RTTTL_SOUND_ONLY = 0
    RTTTL_SOUND_VIBRATE = 1
    RTTTL_VIBRATE_ONLY = 2

    RTTTL_TETRIS = (
        "tetris:d=4,o=5,b=160:"
        "e6,8b,8c6,8d6,16e6,16d6,8c6,8b,a,8a,8c6,e6,8d6,8c6,"
        "b,8b,8c6,d6,e6,c6,a,2a,8p,"
        "d6,8f6,a6,8g6,8f6,e6,8e6,8c6,e6,8d6,8c6,"
        "b,8b,8c6,d6,e6,c6,a,a"
    )
    RTTTL_GAME_OVER = "smbdeath:d=4,o=5,b=90:8p,16b,16f6,16p,16f6,16f.6,16e.6,16d6,16c6,16p,16e,16p,16c,4p"
    RTTTL_LEVEL_UP = "levelup:d=16,o=5,b=200:c,e,g,c6,8p,g,c6,e6,8g6"

    def __init__(self):
        self._setup_functions()
        self._initialized = False

    def _setup_functions(self):
        _lib.pager_init.argtypes = []
        _lib.pager_init.restype = c_int
        _lib.pager_cleanup.argtypes = []
        _lib.pager_cleanup.restype = None

        _lib.pager_set_rotation.argtypes = [c_int]
        _lib.pager_set_rotation.restype = None
        _lib.pager_get_width.argtypes = []
        _lib.pager_get_width.restype = c_int
        _lib.pager_get_height.argtypes = []
        _lib.pager_get_height.restype = c_int

        _lib.pager_flip.argtypes = []
        _lib.pager_flip.restype = None
        _lib.pager_clear.argtypes = [c_uint16]
        _lib.pager_clear.restype = None
        _lib.pager_get_ticks.argtypes = []
        _lib.pager_get_ticks.restype = c_uint32
        _lib.pager_delay.argtypes = [c_uint32]
        _lib.pager_delay.restype = None
        _lib.pager_frame_sync.argtypes = []
        _lib.pager_frame_sync.restype = c_uint32

        _lib.pager_set_pixel.argtypes = [c_int, c_int, c_uint16]
        _lib.pager_set_pixel.restype = None
        _lib.pager_fill_rect.argtypes = [c_int, c_int, c_int, c_int, c_uint16]
        _lib.pager_fill_rect.restype = None
        _lib.pager_draw_rect.argtypes = [c_int, c_int, c_int, c_int, c_uint16]
        _lib.pager_draw_rect.restype = None
        _lib.pager_hline.argtypes = [c_int, c_int, c_int, c_uint16]
        _lib.pager_hline.restype = None
        _lib.pager_vline.argtypes = [c_int, c_int, c_int, c_uint16]
        _lib.pager_vline.restype = None
        _lib.pager_draw_line.argtypes = [c_int, c_int, c_int, c_int, c_uint16]
        _lib.pager_draw_line.restype = None
        _lib.pager_fill_circle.argtypes = [c_int, c_int, c_int, c_uint16]
        _lib.pager_fill_circle.restype = None
        _lib.pager_draw_circle.argtypes = [c_int, c_int, c_int, c_uint16]
        _lib.pager_draw_circle.restype = None

        _lib.pager_draw_char.argtypes = [c_int, c_int, c_char, c_uint16, c_int]
        _lib.pager_draw_char.restype = c_int
        _lib.pager_draw_text.argtypes = [c_int, c_int, c_char_p, c_uint16, c_int]
        _lib.pager_draw_text.restype = c_int
        _lib.pager_draw_text_centered.argtypes = [c_int, c_char_p, c_uint16, c_int]
        _lib.pager_draw_text_centered.restype = None
        _lib.pager_text_width.argtypes = [c_char_p, c_int]
        _lib.pager_text_width.restype = c_int
        _lib.pager_draw_number.argtypes = [c_int, c_int, c_int, c_uint16, c_int]
        _lib.pager_draw_number.restype = c_int

        _lib.pager_draw_ttf.argtypes = [c_int, c_int, c_char_p, c_uint16, c_char_p, c_float]
        _lib.pager_draw_ttf.restype = c_int
        _lib.pager_ttf_width.argtypes = [c_char_p, c_char_p, c_float]
        _lib.pager_ttf_width.restype = c_int
        _lib.pager_ttf_height.argtypes = [c_char_p, c_float]
        _lib.pager_ttf_height.restype = c_int
        _lib.pager_draw_ttf_centered.argtypes = [c_int, c_char_p, c_uint16, c_char_p, c_float]
        _lib.pager_draw_ttf_centered.restype = None
        _lib.pager_draw_ttf_right.argtypes = [c_int, c_char_p, c_uint16, c_char_p, c_float, c_int]
        _lib.pager_draw_ttf_right.restype = None
        _lib.pager_ttf_cleanup.argtypes = []
        _lib.pager_ttf_cleanup.restype = None

        _lib.pager_play_rtttl.argtypes = [c_char_p]
        _lib.pager_play_rtttl.restype = None
        _lib.pager_play_rtttl_ex.argtypes = [c_char_p, c_int]
        _lib.pager_play_rtttl_ex.restype = None
        _lib.pager_stop_audio.argtypes = []
        _lib.pager_stop_audio.restype = None
        _lib.pager_audio_playing.argtypes = []
        _lib.pager_audio_playing.restype = c_int
        _lib.pager_beep.argtypes = [c_int, c_int]
        _lib.pager_beep.restype = None
        _lib.pager_play_rtttl_sync.argtypes = [c_char_p, c_int]
        _lib.pager_play_rtttl_sync.restype = None

        _lib.pager_vibrate.argtypes = [c_int]
        _lib.pager_vibrate.restype = None
        _lib.pager_vibrate_pattern.argtypes = [c_char_p]
        _lib.pager_vibrate_pattern.restype = None

        _lib.pager_led_set.argtypes = [c_char_p, c_int]
        _lib.pager_led_set.restype = None
        _lib.pager_led_rgb.argtypes = [c_char_p, c_uint8, c_uint8, c_uint8]
        _lib.pager_led_rgb.restype = None
        _lib.pager_led_dpad.argtypes = [c_char_p, c_uint32]
        _lib.pager_led_dpad.restype = None
        _lib.pager_led_all_off.argtypes = []
        _lib.pager_led_all_off.restype = None

        _lib.pager_random.argtypes = [c_int]
        _lib.pager_random.restype = c_int
        _lib.pager_seed_random.argtypes = [c_uint32]
        _lib.pager_seed_random.restype = None

        _lib.pager_wait_button.argtypes = []
        _lib.pager_wait_button.restype = c_int
        _lib.pager_poll_input.argtypes = [POINTER(PagerInput)]
        _lib.pager_poll_input.restype = None

        _lib.pager_get_input_event.argtypes = [POINTER(PagerInputEvent)]
        _lib.pager_get_input_event.restype = c_int
        _lib.pager_has_input_events.argtypes = []
        _lib.pager_has_input_events.restype = c_int
        _lib.pager_peek_buttons.argtypes = []
        _lib.pager_peek_buttons.restype = c_uint8
        _lib.pager_clear_input_events.argtypes = []
        _lib.pager_clear_input_events.restype = None

        _lib.pager_set_brightness.argtypes = [c_int]
        _lib.pager_set_brightness.restype = c_int
        _lib.pager_get_brightness.argtypes = []
        _lib.pager_get_brightness.restype = c_int
        _lib.pager_get_max_brightness.argtypes = []
        _lib.pager_get_max_brightness.restype = c_int
        _lib.pager_screen_off.argtypes = []
        _lib.pager_screen_off.restype = c_int
        _lib.pager_screen_on.argtypes = []
        _lib.pager_screen_on.restype = c_int

        _lib.pager_load_image.argtypes = [c_char_p]
        _lib.pager_load_image.restype = c_void_p
        _lib.pager_free_image.argtypes = [c_void_p]
        _lib.pager_free_image.restype = None
        _lib.pager_draw_image.argtypes = [c_int, c_int, c_void_p]
        _lib.pager_draw_image.restype = None
        _lib.pager_draw_image_scaled.argtypes = [c_int, c_int, c_int, c_int, c_void_p]
        _lib.pager_draw_image_scaled.restype = None
        _lib.pager_draw_image_file.argtypes = [c_int, c_int, c_char_p]
        _lib.pager_draw_image_file.restype = c_int
        _lib.pager_draw_image_file_scaled.argtypes = [c_int, c_int, c_int, c_int, c_char_p]
        _lib.pager_draw_image_file_scaled.restype = c_int
        _lib.pager_get_image_info.argtypes = [c_char_p, POINTER(c_int), POINTER(c_int)]
        _lib.pager_get_image_info.restype = c_int
        _lib.pager_draw_image_scaled_rotated.argtypes = [c_int, c_int, c_int, c_int, c_void_p, c_int]
        _lib.pager_draw_image_scaled_rotated.restype = None
        _lib.pager_draw_image_file_scaled_rotated.argtypes = [c_int, c_int, c_int, c_int, c_char_p, c_int]
        _lib.pager_draw_image_file_scaled_rotated.restype = c_int
        _lib.pager_screenshot.argtypes = [c_char_p, c_int]
        _lib.pager_screenshot.restype = c_int

    def init(self):
        result = _lib.pager_init()
        if result == 0:
            self._initialized = True
        return result

    def cleanup(self):
        if self._initialized:
            _lib.pager_cleanup()
            self._initialized = False

    def set_rotation(self, rotation):
        _lib.pager_set_rotation(rotation)

    @property
    def width(self):
        return _lib.pager_get_width()

    @property
    def height(self):
        return _lib.pager_get_height()

    def flip(self):
        _lib.pager_flip()

    def clear(self, color=0):
        _lib.pager_clear(color)

    def get_ticks(self):
        return _lib.pager_get_ticks()

    def delay(self, ms):
        _lib.pager_delay(ms)

    def frame_sync(self):
        return _lib.pager_frame_sync()

    @staticmethod
    def rgb(r, g, b):
        return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)

    @staticmethod
    def hex_color(rgb_hex):
        r = (rgb_hex >> 16) & 0xFF
        g = (rgb_hex >> 8) & 0xFF
        b = rgb_hex & 0xFF
        return Pager.rgb(r, g, b)

    def pixel(self, x, y, color):
        _lib.pager_set_pixel(x, y, color)

    def fill_rect(self, x, y, w, h, color):
        _lib.pager_fill_rect(x, y, w, h, color)

    def rect(self, x, y, w, h, color):
        _lib.pager_draw_rect(x, y, w, h, color)

    def hline(self, x, y, w, color):
        _lib.pager_hline(x, y, w, color)

    def vline(self, x, y, h, color):
        _lib.pager_vline(x, y, h, color)

    def line(self, x0, y0, x1, y1, color):
        _lib.pager_draw_line(x0, y0, x1, y1, color)

    def fill_circle(self, cx, cy, r, color):
        _lib.pager_fill_circle(cx, cy, r, color)

    def circle(self, cx, cy, r, color):
        _lib.pager_draw_circle(cx, cy, r, color)

    def draw_char(self, x, y, char, color, size=1):
        return _lib.pager_draw_char(x, y, char.encode(), color, size)

    def draw_text(self, x, y, text, color, size=1):
        return _lib.pager_draw_text(x, y, text.encode(), color, size)

    def draw_text_centered(self, y, text, color, size=1):
        _lib.pager_draw_text_centered(y, text.encode(), color, size)

    def text_width(self, text, size=1):
        return _lib.pager_text_width(text.encode(), size)

    def draw_number(self, x, y, num, color, size=1):
        return _lib.pager_draw_number(x, y, num, color, size)

    def draw_ttf(self, x, y, text, color, font_path, font_size):
        return _lib.pager_draw_ttf(x, y, text.encode(), color, font_path.encode(), font_size)

    def ttf_width(self, text, font_path, font_size):
        return _lib.pager_ttf_width(text.encode(), font_path.encode(), font_size)

    def ttf_height(self, font_path, font_size):
        return _lib.pager_ttf_height(font_path.encode(), font_size)

    def draw_ttf_centered(self, y, text, color, font_path, font_size):
        _lib.pager_draw_ttf_centered(y, text.encode(), color, font_path.encode(), font_size)

    def draw_ttf_right(self, y, text, color, font_path, font_size, padding=0):
        _lib.pager_draw_ttf_right(y, text.encode(), color, font_path.encode(), font_size, padding)

    def play_rtttl(self, melody, mode=None):
        if mode is None:
            _lib.pager_play_rtttl(melody.encode())
        else:
            _lib.pager_play_rtttl_ex(melody.encode(), mode)

    def stop_audio(self):
        _lib.pager_stop_audio()

    def audio_playing(self):
        return bool(_lib.pager_audio_playing())

    def beep(self, freq, duration_ms):
        _lib.pager_beep(freq, duration_ms)

    def play_rtttl_sync(self, melody, with_vibration=False):
        _lib.pager_play_rtttl_sync(melody.encode(), 1 if with_vibration else 0)

    def vibrate(self, duration_ms=200):
        _lib.pager_vibrate(duration_ms)

    def vibrate_pattern(self, pattern):
        _lib.pager_vibrate_pattern(pattern.encode())

    def led_set(self, name, brightness):
        _lib.pager_led_set(name.encode(), brightness)

    def led_rgb(self, button, r, g, b):
        _lib.pager_led_rgb(button.encode(), r, g, b)

    def led_dpad(self, direction, color):
        _lib.pager_led_dpad(direction.encode(), color)

    def led_all_off(self):
        _lib.pager_led_all_off()

    def random(self, max_val):
        return _lib.pager_random(max_val)

    def seed_random(self, seed):
        _lib.pager_seed_random(seed)

    def wait_button(self):
        return _lib.pager_wait_button()

    def poll_input(self):
        state = PagerInput()
        _lib.pager_poll_input(byref(state))
        return state.current, state.pressed, state.released

    def get_input_event(self):
        event = PagerInputEvent()
        if _lib.pager_get_input_event(byref(event)):
            return (event.button, event.type, event.timestamp)
        return None

    def has_input_events(self):
        return bool(_lib.pager_has_input_events())

    def peek_buttons(self):
        return _lib.pager_peek_buttons()

    def clear_input_events(self):
        _lib.pager_clear_input_events()

    def set_brightness(self, percent):
        return _lib.pager_set_brightness(percent)

    def get_brightness(self):
        return _lib.pager_get_brightness()

    def get_max_brightness(self):
        return _lib.pager_get_max_brightness()

    def screen_off(self):
        return _lib.pager_screen_off()

    def screen_on(self):
        return _lib.pager_screen_on()

    def load_image(self, filepath):
        handle = _lib.pager_load_image(filepath.encode())
        return handle if handle else None

    def free_image(self, handle):
        if handle:
            _lib.pager_free_image(handle)

    def draw_image(self, x, y, handle):
        if handle:
            _lib.pager_draw_image(x, y, handle)

    def draw_image_scaled(self, x, y, w, h, handle):
        if handle:
            _lib.pager_draw_image_scaled(x, y, w, h, handle)

    def draw_image_file(self, x, y, filepath):
        return _lib.pager_draw_image_file(x, y, filepath.encode())

    def draw_image_file_scaled(self, x, y, w, h, filepath):
        return _lib.pager_draw_image_file_scaled(x, y, w, h, filepath.encode())

    def get_image_info(self, filepath):
        w = c_int()
        h = c_int()
        if _lib.pager_get_image_info(filepath.encode(), byref(w), byref(h)) == 0:
            return (w.value, h.value)
        return None

    def draw_image_scaled_rotated(self, x, y, w, h, handle, rotation=0):
        if handle:
            _lib.pager_draw_image_scaled_rotated(x, y, w, h, handle, rotation)

    def draw_image_file_scaled_rotated(self, x, y, w, h, filepath, rotation=0):
        return _lib.pager_draw_image_file_scaled_rotated(x, y, w, h, filepath.encode(), rotation)

    def screenshot(self, filepath, rotation=270):
        return _lib.pager_screenshot(filepath.encode(), rotation)

    def __enter__(self):
        self.init()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False


if __name__ == "__main__":
    with Pager() as p:
        p.set_rotation(270)
        p.clear(p.rgb(0, 0, 32))
        p.draw_text_centered(100, "libpagerctl.so", p.WHITE, 2)
        p.draw_text_centered(130, "Python Demo", p.CYAN, 1)
        p.flip()
        p.beep(800, 100)
        p.vibrate(100)
        p.delay(3000)
