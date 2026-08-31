import logging
import os
import sys
import threading
import time

_LIB_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        '..', '..', 'lib'))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from pagerctl import Pager

ROTATION = 270


class DisplayError(RuntimeError):
    pass


METRIC_CACHE_MAX = 1024


class Display:
    def __init__(self):
        self.lock = threading.RLock()
        self.pager = Pager()
        self.alive = False
        self.width = 480
        self.height = 222
        self._metrics = {}

    def open(self):
        if self.alive:
            return
        try:
            started = self.pager.init()
        except Exception as e:
            raise DisplayError('pager_init() failed: %s' % e)
        if started != 0:
            raise DisplayError('pager_init() failed - display unavailable')
        self.pager.set_rotation(ROTATION)
        self.alive = True
        self.width = self.pager.width
        self.height = self.pager.height

    def close(self):
        with self.lock:
            if not self.alive:
                return
            try:
                self.pager.led_all_off()
            except Exception:
                pass
            self.alive = False
            try:
                self.pager.cleanup()
            except Exception as e:
                logging.debug('display cleanup: %s', e)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        pager = self.__dict__.get('pager')
        if pager is None:
            raise AttributeError(name)
        attr = getattr(pager, name)
        if not callable(attr):
            return attr

        lock = self.__dict__['lock']

        def guarded(*args, **kw):
            if not self.__dict__.get('alive'):
                return None
            with lock:
                if not self.__dict__.get('alive'):
                    return None
                try:
                    return attr(*args, **kw)
                except Exception as e:
                    logging.debug('display call %s: %s', name, e)
                    return None
        self.__dict__[name] = guarded
        return guarded

    def _measure(self, name, key, *args):
        if not self.alive:
            return 0
        with self.lock:
            if not self.alive:
                return 0
            try:
                value = getattr(self.pager, name)(*args) or 0
            except Exception as e:
                logging.debug('display call %s: %s', name, e)
                return 0
            if len(self._metrics) >= METRIC_CACHE_MAX:
                self._metrics.clear()
            self._metrics[key] = value
        return value

    def _cached(self, key):
        with self.lock:
            return self._metrics.get(key)

    def ttf_width(self, text, font, size):
        key = (text, font, size)
        cached = self._cached(key)
        if cached is None:
            return self._measure('ttf_width', key, text, font, size)
        return cached

    def ttf_height(self, font, size):
        key = (None, font, size)
        cached = self._cached(key)
        if cached is None:
            return self._measure('ttf_height', key, font, size)
        return cached

    def brightness(self, percent):
        with self.lock:
            self.set_brightness(max(0, min(100, int(percent))))

    def frame(self):
        return _Frame(self)


class _Frame:
    def __init__(self, display):
        self.display = display

    def __enter__(self):
        self.display.lock.acquire()
        return self.display

    def __exit__(self, exc_type, *rest):
        try:
            if exc_type is None:
                self.display.flip()
        finally:
            self.display.lock.release()
        return False


BUTTONS = {
    Pager.BTN_UP: 'up',
    Pager.BTN_DOWN: 'down',
    Pager.BTN_LEFT: 'left',
    Pager.BTN_RIGHT: 'right',
    Pager.BTN_A: 'select',
    Pager.BTN_B: 'back',
}


ACTIVE_POLL = 0.016
IDLE_POLL = 0.045
ACTIVE_WINDOW = 1.5


class Input:
    def __init__(self, display):
        self.display = display
        self._last_press = 0.0

    def drain(self):
        self.display.poll_input()
        self.display.clear_input_events()

    def poll(self):
        if not self.display.alive:
            return None
        self.display.poll_input()
        if not self.display.has_input_events():
            return None
        event = self.display.get_input_event()
        if not event:
            return None
        button, kind, _ = event
        if kind != Pager.EVENT_PRESS:
            return None
        return BUTTONS.get(button)

    def _nap(self, idle):
        if idle is not None:
            return idle
        if time.monotonic() - self._last_press < ACTIVE_WINDOW:
            return ACTIVE_POLL
        return IDLE_POLL

    def wait(self, timeout=None, idle=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            name = self.poll()
            if name:
                self._last_press = time.monotonic()
                return name
            if deadline is not None and time.monotonic() > deadline:
                return None
            if not self.display.alive:
                return None
            time.sleep(self._nap(idle))
