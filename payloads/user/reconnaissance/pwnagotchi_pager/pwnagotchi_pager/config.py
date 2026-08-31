import configparser
import copy
import json
import logging
import logging.handlers
import os
import time
import shutil
import threading

from . import interfaces, system

PAYLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
DATA_DIR = os.path.join(PAYLOAD_DIR, 'data')
FONTS_DIR = os.path.join(PAYLOAD_DIR, 'fonts')
CONFIG_FILE = os.path.join(PAYLOAD_DIR, 'config.conf')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
SESSION_FILE = os.path.join(DATA_DIR, 'session.json')
CAPTURE_CACHE = os.path.join(DATA_DIR, 'capture_index.json')
PINEAPD_ARGV = os.path.join(DATA_DIR, 'pineapd.argv')
NO_INJECT_FILE = os.path.join(DATA_DIR, 'no_inject.json')
LEDGER_FILE = os.path.join(DATA_DIR, 'hunt_ledger.json')
APPROACH_FILE = os.path.join(DATA_DIR, 'approach.json')
YIELD_FILE = os.path.join(DATA_DIR, 'yield.json')
LOG_FILE = os.path.join(DATA_DIR, 'pwnagotchi_pager.log')

LOOT_DIR = '/root/loot/PwnagotchiPager'
LEGACY_LOOT_DIRS = ['/root/loot/Pagergotchi']
HANDSHAKES_DIR = os.path.join(LOOT_DIR, 'handshakes')
AP_LOG_DIR = os.path.join(LOOT_DIR, 'ap_logs')


CAPTURE_SUFFIXES = ('.22000', '.pcap', '.pcapng', '.nohash')

LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 2


def _capture_files(path):
    try:
        return [f for f in os.listdir(path) if f.endswith(CAPTURE_SUFFIXES)]
    except OSError:
        return []


def adopt_legacy_loot(target):
    moved = 0
    for legacy in LEGACY_LOOT_DIRS:
        source = os.path.join(legacy, 'handshakes')
        if not os.path.isdir(source) or os.path.abspath(source) == os.path.abspath(target):
            continue
        names = _capture_files(source)
        if not names:
            continue
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            logging.warning('could not create %s: %s', target, e)
            return 0
        for name in names:
            src = os.path.join(source, name)
            dst = os.path.join(target, name)
            if os.path.exists(dst):
                continue
            try:
                os.rename(src, dst)
                moved += 1
            except OSError:
                try:
                    shutil.copy2(src, dst)
                    os.remove(src)
                    moved += 1
                except Exception as e:
                    logging.debug('could not adopt %s: %s', name, e)
        if moved:
            logging.warning('adopted %d capture file(s) from %s', moved, source)
    return moved


def resolve_handshakes_dir(configured=None):
    configured = (configured or '').strip()
    if not configured:
        return HANDSHAKES_DIR
    path = os.path.normpath(configured)
    if not os.path.isabs(path):
        logging.warning('handshakes_dir %r is not an absolute path, '
                        'using %s', configured, HANDSHAKES_DIR)
        return HANDSHAKES_DIR
    return path

MIN_CHANNEL = 1
MAX_CHANNEL = 233

THEME_NAMES = ['Abyss', 'Ember', 'Orchid', 'Moss', 'Slate']
AUTO_DIM_OPTIONS = [0, 30, 60, 120]
AUTO_DIM_LEVELS = [10, 20, 30, 40, 50]
BRIGHTNESS_STEPS = list(range(20, 101, 10))

DEFAULTS = {
    'iface_choice': interfaces.AUTO,
    'deauth_enabled': True,
    'skip_captured': True,
    'cover_siblings': True,
    'single_pmkid': True,
    'pool_enabled': True,
    'log_aps_enabled': False,
    'leds_enabled': True,
    'haptics_enabled': True,
    'sound_enabled': False,
    'whitelist': [],
    'blacklist': [],
    'theme': 'Abyss',
    'brightness': 100,
    'auto_dim': 0,
    'auto_dim_level': 20,
    'config_imported': '',
    'config_ssids': [],
}

TUNING = {
    'recon_time': 30,
    'recon_inactive_multiplier': 2,
    'max_inactive_scale': 2,
    'hop_recon_time': 10,
    'min_recon_time': 5,
    'recon_settle': 8,
    'dwell_time': 4,
    'examine_seconds': 6,
    'ap_ttl': 120,
    'min_attack_rssi': -85,
    'max_interactions': 3,
    'max_misses_for_recon': 10,
    'bored_epochs': 5,
    'sad_epochs': 10,
    'excited_epochs': 10,
    'max_blind_epochs': 50,
    'throttle_assoc': 0.4,
    'throttle_deauth': 0.9,
    'deauth_bursts': 3,
    'deauth_burst_gap': 0.12,
    'reconnect_dwell': 6,
    'reconnect_dwell_per_ap': 1.5,
    'max_reconnect_dwell': 25,
    'stubborn_epochs': 4,
    'max_effort': 3,
    'rssi_adapt': 8,
    'rssi_rich_targets': 5,
    'effort_dwell_bonus': 0.4,
    'max_deauth_refusals': 2,
    'low_disk_mb': 100,
    'critical_disk_mb': 40,
    'targeted_rssi': -78,
    'handshake_rssi': -80,
    'weak_dwell_bonus': 1.6,
    'signal_forgive_db': 6,
    'nearby_clients': 2,
    'crowded_targets': 12,
    'near_rssi': -60,
    'max_channels_per_epoch': 6,
    'deauth_dead_after': 6,
    'loud_channel_bursts': 2,
    'busy_dwell_bonus': 1.3,
    'max_targets_per_channel': 8,
    'radio_recheck_secs': 20,
    'pmkid_sweep_secs': 45,
    'pmkid_sweep_every_epochs': 2,
    'pmkid_sweep_min_secs': 25,
    'pmkid_sweep_max_secs': 75,
    'pool_refresh_epochs': 5,
    'fps': 2.0,
}


def _conf_number(cp, section, key, fallback, kind=int, low=None, high=None):
    raw = cp.get(section, key, fallback=None)
    if raw is None or not str(raw).strip():
        return fallback
    try:
        value = kind(str(raw).strip())
    except (TypeError, ValueError):
        logging.warning('config.conf: %s.%s is not a number (%r), using %s',
                        section, key, raw, fallback)
        return fallback
    if value != value or value in (float('inf'), float('-inf')):
        logging.warning('config.conf: %s.%s is not a finite number (%r), '
                        'using %s', section, key, raw, fallback)
        return fallback
    if low is not None and value < low:
        logging.warning('config.conf: %s.%s raised from %s to %s',
                        section, key, value, low)
        value = low
    if high is not None and value > high:
        logging.warning('config.conf: %s.%s lowered from %s to %s',
                        section, key, value, high)
        value = high
    return value


def _conf_flag(cp, section, key, fallback):
    raw = cp.get(section, key, fallback=None)
    if raw is None or not str(raw).strip():
        return fallback
    text = str(raw).strip().lower()
    if text in ('1', 'yes', 'true', 'on'):
        return True
    if text in ('0', 'no', 'false', 'off'):
        return False
    logging.warning('config.conf: %s.%s is not yes or no (%r), using %s',
                    section, key, raw, fallback)
    return fallback


def foreign_loot_dirs(own):
    seen, out = {os.path.normpath(own)} if own else set(), []
    for key in ('pineapd.@pineapd[0].handshakepath',):
        rc, path, _ = system.run_cmd(['uci', 'get', key], timeout=5)
        if rc != 0:
            continue
        path = os.path.normpath(path.strip())
        if not path or path in seen or not os.path.isdir(path):
            continue
        seen.add(path)
        out.append(path)
    return out


def sweep_scratch(older_than=3600):
    import glob
    cutoff = time.time() - older_than
    dropped = 0
    for junk in glob.glob(os.path.join(DATA_DIR, '*.tmp')):
        try:
            if os.path.getmtime(junk) < cutoff:
                os.remove(junk)
                dropped += 1
        except OSError:
            continue
    if dropped:
        logging.info('cleared %d abandoned scratch file(s)', dropped)
    return dropped


def ensure_loot_dir(preferred):
    tried = []
    for candidate in (preferred, HANDSHAKES_DIR):
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, '.writable')
            with open(probe, 'w'):
                pass
            os.remove(probe)
        except OSError as e:
            logging.warning('cannot store captures in %s: %s', candidate, e)
            continue
        if candidate != preferred:
            logging.warning('storing captures in %s instead', candidate)
        return candidate
    raise RuntimeError('no writable capture directory (tried %s)'
                       % ', '.join(tried))


def _coerce_list(value):
    out = []
    for entry in value if isinstance(value, list) else []:
        if isinstance(entry, str):
            entry = {'ssid': entry, 'bssid': ''}
        if not isinstance(entry, dict):
            continue
        ssid = str(entry.get('ssid', '') or '').strip()
        bssid = str(entry.get('bssid', '') or '').strip()
        if ssid or bssid:
            out.append({'ssid': ssid, 'bssid': bssid})
    return out


def _coerce_choice(value, options, fallback):
    return value if value in options else fallback


class Settings:
    def __init__(self, config_file=CONFIG_FILE, settings_file=SETTINGS_FILE):
        self._config_file = config_file
        self._settings_file = settings_file
        self._lock = threading.RLock()
        self._listeners = []
        self._sig = None
        self.values = dict(DEFAULTS)
        self.tuning = dict(TUNING)
        self.debug = False
        self.channels = []
        self.handshakes_dir = HANDSHAKES_DIR
        self.config_ssids = []
        self.configured = set()
        self.load()

    def on_change(self, cb):
        if cb not in self._listeners:
            self._listeners.append(cb)

    def off_change(self, cb):
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _notify(self):
        for cb in list(self._listeners):
            try:
                cb(self)
            except Exception as e:
                logging.debug('settings listener failed: %s', e)

    def _read_config_file(self):
        try:
            self._parse_config_file()
        except Exception as e:
            logging.warning('could not use %s (%s), using defaults',
                            self._config_file, e)
            self.handshakes_dir = resolve_handshakes_dir()

    def _parse_config_file(self):
        configured = ''
        self.configured = set()
        self.tuning = dict(TUNING)
        self.channels = []
        self.config_ssids = []
        self.debug = False
        cp = configparser.ConfigParser(interpolation=None)
        if not os.path.exists(self._config_file):
            self.handshakes_dir = resolve_handshakes_dir()
            return
        try:
            cp.read(self._config_file)
        except Exception as e:
            logging.warning('could not read %s: %s', self._config_file, e)
            self.handshakes_dir = resolve_handshakes_dir()
            return
        if cp.has_section('general'):
            self.debug = _conf_flag(cp, 'general', 'debug', False)
        if cp.has_section('capture'):
            configured = cp.get('capture', 'handshakes_dir', fallback='')
            for key in ('pmkid_sweep_secs', 'pmkid_sweep_every_epochs'):
                if (cp.get('capture', key, fallback='') or '').strip():
                    self.configured.add(key)
            self.tuning['pmkid_sweep_secs'] = _conf_number(
                cp, 'capture', 'pmkid_sweep_secs', TUNING['pmkid_sweep_secs'],
                int, low=5, high=600)
            self.tuning['pmkid_sweep_every_epochs'] = _conf_number(
                cp, 'capture', 'pmkid_sweep_every_epochs',
                TUNING['pmkid_sweep_every_epochs'], int, low=1, high=50)
        self.handshakes_dir = resolve_handshakes_dir(configured)
        if cp.has_section('channels'):
            raw = cp.get('channels', 'channels', fallback='')
            parsed = []
            for chunk in raw.split(','):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    value = int(chunk)
                except ValueError:
                    logging.warning('ignoring invalid channel %r', chunk)
                    continue
                if not MIN_CHANNEL <= value <= MAX_CHANNEL:
                    logging.warning('ignoring out-of-range channel %d '
                                    '(expected %d-%d)', value,
                                    MIN_CHANNEL, MAX_CHANNEL)
                    continue
                parsed.append(value)
            self.channels = parsed
        if cp.has_section('timing'):
            self.tuning['throttle_deauth'] = _conf_number(
                cp, 'timing', 'throttle_d', TUNING['throttle_deauth'],
                float, low=0.0, high=30.0)
            self.tuning['throttle_assoc'] = _conf_number(
                cp, 'timing', 'throttle_a', TUNING['throttle_assoc'],
                float, low=0.0, high=30.0)
        if cp.has_section('whitelist'):
            raw = cp.get('whitelist', 'ssids', fallback='')
            self.config_ssids = [s.strip() for s in raw.split(',') if s.strip()]

    def _one_sig(self, path):
        try:
            st = os.stat(path)
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def _file_sig(self):
        return (self._one_sig(self._settings_file),
                self._one_sig(self._config_file))

    def load(self):
        with self._lock:
            self._read_config_file()
            values = dict(DEFAULTS)
            sig = self._file_sig()
            if sig[0] is not None:
                try:
                    with open(self._settings_file) as f:
                        saved = json.load(f)
                    if isinstance(saved, dict):
                        values.update(saved)
                except Exception as e:
                    logging.warning('could not read %s: %s', self._settings_file, e)
            self._sig = sig
            self.values = self._validate(values)
            imported = self._apply_config_ssids()
            values = self.values
        if imported:
            self.save(notify=False)
        return values

    def _validate(self, values):
        values = {k: v for k, v in values.items() if k in DEFAULTS}
        for key, fallback in DEFAULTS.items():
            values.setdefault(key, fallback)
        values['whitelist'] = _coerce_list(values.get('whitelist'))
        values['blacklist'] = _coerce_list(values.get('blacklist'))
        stored = values.get('config_ssids')
        values['config_ssids'] = [s for s in (stored if isinstance(stored, list)
                                              else []) if isinstance(s, str)]
        values['theme'] = _coerce_choice(values.get('theme'), THEME_NAMES,
                                    THEME_NAMES[0])
        values['auto_dim'] = _coerce_choice(values.get('auto_dim'), AUTO_DIM_OPTIONS, 0)
        values['auto_dim_level'] = _coerce_choice(values.get('auto_dim_level'),
                                                  AUTO_DIM_LEVELS, 20)
        values['iface_choice'] = _coerce_choice(values.get('iface_choice'),
                                                interfaces.CHOICES, interfaces.AUTO)
        try:
            values['brightness'] = max(20, min(100, int(values.get('brightness', 100))))
        except (TypeError, ValueError):
            values['brightness'] = 100
        for key in ('deauth_enabled', 'skip_captured', 'cover_siblings',
                    'single_pmkid', 'pool_enabled', 'log_aps_enabled',
                    'leds_enabled', 'haptics_enabled', 'sound_enabled'):
            values[key] = bool(values.get(key, DEFAULTS[key]))
        values['config_imported'] = str(values.get('config_imported') or '')
        return values

    def _apply_config_ssids(self):
        signature = '|'.join(sorted(self.config_ssids))
        if self.values.get('config_imported') == signature:
            return False
        from . import scope
        previous = [s for s in self.values.get('config_ssids') or []
                    if isinstance(s, str)]
        wanted = list(self.config_ssids)
        gone = [s for s in previous if s not in wanted]
        wl = self.values['whitelist']
        removed = 0
        if gone:
            keep = []
            for entry in wl:
                name = (entry.get('ssid') or '').strip()
                if name and not entry.get('bssid') and name in gone:
                    removed += 1
                    continue
                keep.append(entry)
            wl = keep
            self.values['whitelist'] = wl
        added = 0
        for ssid in wanted:
            if not scope.contains(wl, ssid):
                wl.append({'ssid': ssid, 'bssid': ''})
                added += 1
        self.values['config_imported'] = signature
        self.values['config_ssids'] = wanted
        if added or removed:
            logging.info('config.conf whitelist: %d added, %d removed',
                         added, removed)
        return True

    def reload_if_changed(self):
        if self._file_sig() == self._sig:
            return False
        self.load()
        self._notify()
        return True

    def get(self, key, default=None):
        with self._lock:
            value = self.values.get(key, default)
            return copy.deepcopy(value) if isinstance(value, (list, dict)) else value

    def set(self, key, value, save=True):
        with self._lock:
            self.values[key] = value
            self.values = self._validate(self.values)
            if save:
                self.save(notify=False)
        self._notify()
        return self.values[key]

    def toggle(self, key):
        return self.set(key, not self.get(key))

    def cycle(self, key, options, step=1):
        current = self.get(key)
        try:
            idx = options.index(current)
        except ValueError:
            idx = 0
        return self.set(key, options[(idx + step) % len(options)])

    def save(self, notify=True):
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self._settings_file), exist_ok=True)
                tmp = self._settings_file + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(self.values, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._settings_file)
                self._sig = self._file_sig()
            except Exception as e:
                logging.warning('could not save settings: %s', e)
        if notify:
            self._notify()

    def tune(self, key):
        return self.tuning[key]

    def __getitem__(self, key):
        return self.get(key)


def setup_logging(settings):
    level = logging.DEBUG if settings.debug else logging.WARNING
    logging.basicConfig(level=level,
                        format='[%(asctime)s] [%(levelname)s] %(message)s',
                        datefmt='%H:%M:%S')
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(logging.NOTSET)
    if not settings.debug:
        return
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        logging.getLogger().addHandler(handler)
        logging.info('debug logging to %s', LOG_FILE)
    except Exception as e:
        logging.warning('could not open log file: %s', e)
