import glob
import logging
import os
import subprocess
import threading
import time

_STARTED_AT = time.monotonic()


def uptime():
    return time.monotonic() - _STARTED_AT


def cpu_load():
    try:
        with open('/proc/loadavg') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _meminfo():
    fields = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                key, _, rest = line.partition(':')
                parts = rest.split()
                if parts:
                    fields[key] = int(parts[0])
    except Exception:
        pass
    return fields


def mem_usage():
    fields = _meminfo()
    total = fields.get('MemTotal', 0)
    if total <= 0:
        return 0.0
    if 'MemAvailable' in fields:
        used = total - fields['MemAvailable']
    else:
        used = total - (fields.get('MemFree', 0) + fields.get('Buffers', 0)
                        + fields.get('Cached', 0))
    return max(0.0, min(100.0, used * 100.0 / total))


def temperature():
    for path in ('/sys/class/thermal/thermal_zone0/temp',
                 '/sys/class/hwmon/hwmon0/temp1_input'):
        try:
            with open(path) as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            continue
    return 0.0


def _power_supplies(leaf):
    for path in sorted(glob.glob('/sys/class/power_supply/*/' + leaf)):
        try:
            kind = os.path.join(os.path.dirname(path), 'type')
            if os.path.exists(kind):
                with open(kind) as f:
                    if f.read().strip().lower() != 'battery':
                        continue
            with open(path) as f:
                yield f.read().strip()
        except Exception:
            continue


def battery():
    for value in _power_supplies('capacity'):
        try:
            return max(0, min(100, int(value)))
        except ValueError:
            continue
    for path in ('/sys/devices/platform/battery/capacity',
                 '/sys/devices/platform/axp20x-battery-power-supply/capacity'):
        try:
            with open(path) as f:
                return max(0, min(100, int(f.read().strip())))
        except Exception:
            continue
    return None


def battery_charging():
    answer = None
    for value in _power_supplies('status'):
        if value.lower() in ('charging', 'full'):
            return True
        answer = False
    if answer is not None:
        return answer
    for path in ('/sys/devices/platform/battery/status',
                 '/sys/devices/platform/axp20x-battery-power-supply/status'):
        try:
            with open(path) as f:
                return f.read().strip().lower() in ('charging', 'full')
        except Exception:
            continue
    for path in ('/sys/devices/platform/battery/present',
                 '/sys/class/power_supply/usb/online',
                 '/sys/class/power_supply/ac/online'):
        try:
            with open(path) as f:
                return f.read().strip() == '1'
        except Exception:
            continue
    return None


def disk_free_mb(path):
    try:
        st = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    return st.f_bavail * st.f_frsize / (1024.0 * 1024.0)


def reboot():
    subprocess.run(['reboot'], check=False)


def shutdown():
    subprocess.run(['poweroff'], check=False)


def run_cmd(cmd, timeout=10):
    if isinstance(cmd, str):
        cmd = cmd.split()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (r.returncode,
                r.stdout.decode('utf-8', 'replace').strip(),
                r.stderr.decode('utf-8', 'replace').strip())
    except subprocess.TimeoutExpired:
        logging.debug('timeout: %s', cmd)
        return -1, '', 'timeout'
    except Exception as e:
        logging.debug('failed: %s (%s)', cmd, e)
        return -1, '', str(e)


def have(tool):
    return run_cmd(['which', tool], timeout=5)[0] == 0


def first_line(text):
    for line in str(text or '').splitlines():
        line = line.strip()
        if line:
            return line
    return ''


def secs_to_hhmmss(secs):
    try:
        secs = float(secs)
    except (TypeError, ValueError):
        secs = 0
    if secs != secs or secs in (float('inf'), float('-inf')):
        secs = 0
    mins, secs = divmod(int(max(0, secs)), 60)
    hours, mins = divmod(mins, 60)
    return '%02d:%02d:%02d' % (hours, mins, secs)


IDLE = (0, 0, 40)
HUNTING = (0, 60, 90)
ATTACKING = (90, 40, 0)
CAPTURED = (0, 120, 30)
SAD = (60, 0, 60)
ERROR = (120, 0, 0)

CAPTURE_JINGLE = 'pwnd:d=16,o=6,b=200:c,e,g,c7'
DPAD = ('up', 'right', 'down', 'left')


class Feedback:
    def __init__(self, display, settings):
        self.display = display
        self.settings = settings
        self._lock = threading.Lock()
        self._colour = None
        settings.on_change(self._on_settings)

    def _enabled(self, key):
        return bool(self.settings.get(key))

    def _on_settings(self, settings=None):
        if not self._enabled('leds_enabled') and self._colour is not None:
            self.leds_off()

    def glow(self, colour):
        if not self._enabled('leds_enabled'):
            if self._colour is not None:
                self.leds_off()
            return
        with self._lock:
            if colour == self._colour:
                return
            self._colour = None
            try:
                for name in DPAD:
                    self.display.led_rgb(name, *colour)
                self._colour = colour
            except Exception as e:
                logging.debug('led: %s', e)

    def leds_off(self):
        with self._lock:
            self._colour = None
        try:
            self.display.led_all_off()
        except Exception as e:
            logging.debug('led off: %s', e)

    def buzz(self, ms=120):
        if not self._enabled('haptics_enabled'):
            return
        try:
            self.display.vibrate(ms)
        except Exception as e:
            logging.debug('vibrate: %s', e)

    def jingle(self, melody=CAPTURE_JINGLE):
        if not self._enabled('sound_enabled'):
            return
        try:
            self.display.play_rtttl(melody)
        except Exception as e:
            logging.debug('rtttl: %s', e)

    def on_capture(self, count=1):
        self.glow(CAPTURED)
        self.buzz(200)
        self.jingle()

    def on_attack(self):
        self.glow(ATTACKING)

    def on_hunt(self):
        self.glow(HUNTING)

    def on_idle(self):
        self.glow(IDLE)

    def on_sad(self):
        self.glow(SAD)

    def on_error(self):
        self.glow(ERROR)
        self.buzz(400)

    def shutdown(self):
        self.settings.off_change(self._on_settings)
        self.leds_off()
        try:
            self.display.stop_audio()
        except Exception:
            pass
