import glob
import json
import logging
import os
import threading
import time

from . import config, interfaces, system

OPEN_AP = 'wlan0open'
OPEN_RADIO = 'radio0'
MAX_POOL = 24
MAX_SSID = 32
MAX_SSID_BYTES = 32
UNSAFE_SSID = ''.join(chr(c) for c in list(range(0, 32)) + [127])
RELOAD_SETTLE = 2.0
RELOAD_WAIT = 30.0
TEARDOWN_WAIT = 30.0
POOL_RETRY_AFTER = 300
STATE_FILE = os.path.join(config.DATA_DIR, 'pool_state.json')


def available():
    return system.have('_pineap') and system.have('uci')


def safe_channel(value):
    text = str(value or '').strip()
    if not text:
        return ''
    if text == 'auto':
        return text
    if text.isdigit() and 1 <= int(text) <= 233:
        return text
    logging.warning('ignoring stored radio channel %r', value)
    return ''


def _uci(*args, **kw):
    return system.run_cmd(['uci'] + list(args), timeout=kw.get('timeout', 10))


def _uci_get(key):
    rc, out, _ = _uci('-q', 'get', key)
    return out.strip() if rc == 0 else ''


def _phy_for_device(device):
    path = _uci_get('wireless.%s.path' % device)
    if not path:
        return ''
    for entry in sorted(glob.glob('/sys/class/ieee80211/*')):
        try:
            target = os.path.realpath(os.path.join(entry, 'device'))
        except OSError:
            continue
        if target.endswith(path):
            return os.path.basename(entry)
    return ''


def open_ap_device():
    return _uci_get('wireless.%s.device' % OPEN_AP) or OPEN_RADIO


def open_ap_phy():
    live = interfaces.phy_of(OPEN_AP)
    if live:
        return live
    device = _uci_get('wireless.%s.device' % OPEN_AP)
    return _phy_for_device(device) if device else ''


def reserved_phys(settings):
    try:
        wanted = bool(settings.get('pool_enabled'))
    except Exception:
        wanted = False
    if not wanted:
        return ()
    phy = open_ap_phy()
    return (phy,) if phy else ()


def shares_radio(*ifaces):
    open_phy = open_ap_phy()
    if not open_phy:
        return False
    for iface in ifaces:
        if iface and interfaces.phy_of(iface) == open_phy:
            return True
    return False


def _pool(*args, **kw):
    return system.run_cmd(['_pineap', 'SSIDPOOL'] + [str(a) for a in args],
                          timeout=kw.get('timeout', 15))


def safe_ssid(ssid):
    ssid = str(ssid or '')
    if not ssid or len(ssid) > MAX_SSID:
        return ''
    if ssid.startswith('-'):
        return ''
    if any(c in UNSAFE_SSID for c in ssid):
        return ''
    if not ssid.strip():
        return ''
    try:
        if len(ssid.encode('utf-8')) > MAX_SSID_BYTES:
            return ''
    except UnicodeEncodeError:
        return ''
    return ssid


def wanted_ssids(stations, aps, scope, limit=MAX_POOL):
    in_range = {str(ap.get('hostname') or '').strip().lower()
                for ap in aps or [] if isinstance(ap, dict) and ap.get('hostname')}
    seen = {}
    for info in (stations or {}).values():
        if not isinstance(info, dict):
            continue
        for ssid, when in (info.get('ssids') or {}).items():
            ssid = safe_ssid(str(ssid or '').strip())
            if not ssid:
                continue
            if ssid.lower() in in_range:
                continue
            if scope is not None and not scope.ssid_allowed(ssid):
                continue
            if when > seen.get(ssid, 0):
                seen[ssid] = when
    ranked = sorted(seen.items(), key=lambda kv: -kv[1])
    return [ssid for ssid, _ in ranked[:limit]]


def _loaded_names(out):
    body = (out or '').strip()
    if not (body.startswith('[') and body.endswith(']')):
        return []
    body = body[1:-1].strip()
    if not body:
        return []
    return [name for name in (chunk.strip().strip('"')
                              for chunk in body.split('","')) if name]


class SsidPool:
    def __init__(self, settings, on_reload=None):
        self.settings = settings
        self.on_reload = on_reload
        self._lock = threading.Lock()
        self.running = False
        self.ssids = []
        self._requested = []
        self.reason = ''
        self._raised_ap = False
        self._was_channel = ''
        self._was_device = ''
        self._channel_changed = False
        self.blocked = False
        self._blocked_at = 0.0
        self._was_off = False
        self._touched_config = False

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump({'channel': self._was_channel,
                           'device': self._was_device,
                           'channel_changed': self._channel_changed,
                           'raised': self._raised_ap}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logging.debug('could not save pool state: %s', e)

    def _load_state(self):
        try:
            with open(STATE_FILE) as fh:
                stored = json.load(fh)
        except Exception:
            return {}
        return stored if isinstance(stored, dict) else {}

    def _clear_state(self):
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except OSError as e:
            logging.debug('could not clear pool state: %s', e)

    def _reload(self):
        system.run_cmd(['wifi', 'reload'], timeout=45)
        if self.on_reload:
            try:
                self.on_reload()
            except Exception as e:
                logging.debug('post-reload handler: %s', e)

    def _wait_for_ap(self, want, timeout, should_stop=None):
        deadline = time.monotonic() + timeout
        path = '/sys/class/net/%s' % OPEN_AP
        while time.monotonic() < deadline:
            if os.path.isdir(path) == want:
                return True
            if should_stop and should_stop():
                break
            time.sleep(0.5)
        return os.path.isdir(path) == want

    @property
    def enabled(self):
        return bool(self.settings.get('pool_enabled'))

    def sync_enabled(self):
        on = self.enabled
        if on and self._was_off:
            self._was_off = False
            self.blocked = False
        elif not on:
            self._was_off = True
        return on

    def holding_off(self):
        return (self.blocked
                and time.monotonic() - self._blocked_at < POOL_RETRY_AFTER)

    def cleanup_stale(self):
        if not available():
            return
        state = self._load_state()
        live = os.path.isdir('/sys/class/net/%s' % OPEN_AP)
        leftover = self.advertising()
        enabled = _uci_get('wireless.%s.disabled' % OPEN_AP) == '0'
        stored_channel = safe_channel(state.get('channel'))
        stored_device = str(state.get('device') or '')
        channel_changed = bool(state.get('channel_changed'))
        ours = bool(state.get('raised')) or (live and not enabled)
        if not leftover and not live and not channel_changed and not state:
            return
        logging.warning('an SSID pool was left running, clearing it')
        _pool('DISABLE', timeout=20)
        _pool('CLEAR')
        _uci('-q', 'delete', 'pineapd.@ssidpool[0].ssid')
        _uci('commit', 'pineapd')
        self._was_channel = stored_channel
        self._was_device = stored_device
        self._channel_changed = channel_changed
        self._raised_ap = ours
        if ours or channel_changed:
            self._lower_ap()
        self._clear_state()

    def _raise_ap(self, channel=0, should_stop=None):
        device = _uci_get('wireless.%s.device' % OPEN_AP)
        if not device:
            self.reason = 'no open AP on this radio'
            return False
        current = _uci_get('wireless.%s.disabled' % OPEN_AP)
        was_down = current != '0'
        if channel:
            had = safe_channel(_uci_get('wireless.%s.channel' % device))
            if had != str(channel):
                self._was_channel = had
                self._was_device = device
                self._channel_changed = True
                _uci('set', 'wireless.%s.channel=%d' % (device, channel))
            else:
                channel = 0
        if was_down:
            _uci('set', 'wireless.%s.disabled=0' % OPEN_AP)
        if was_down or channel:
            self._raised_ap = was_down
            self._touched_config = True
            self._save_state()
            _uci('commit', 'wireless')
            self._reload()
            time.sleep(RELOAD_SETTLE)
            self._wait_for_ap(True, RELOAD_WAIT, should_stop)
        return os.path.isdir('/sys/class/net/%s' % OPEN_AP)

    def _disarm_config(self):
        if not self._raised_ap:
            return
        _uci('set', 'wireless.%s.disabled=1' % OPEN_AP)
        _uci('commit', 'wireless')

    def _lower_ap(self):
        if not self._raised_ap and not self._channel_changed:
            return
        raised, self._raised_ap = self._raised_ap, False
        if raised:
            _uci('set', 'wireless.%s.disabled=1' % OPEN_AP)
        if self._channel_changed:
            device = self._was_device or open_ap_device()
            self._was_channel = safe_channel(self._was_channel)
            if self._was_channel:
                _uci('set', 'wireless.%s.channel=%s'
                     % (device, self._was_channel))
            else:
                _uci('-q', 'delete', 'wireless.%s.channel' % device)
            self._was_channel = ''
            self._was_device = ''
            self._channel_changed = False
        _uci('commit', 'wireless')
        self._touched_config = False
        self._reload()
        if not self._wait_for_ap(False, TEARDOWN_WAIT):
            logging.warning('%s is still up after taking the SSID pool down',
                            OPEN_AP)
        self._clear_state()

    def _push(self, ssids):
        self._requested = []
        ssids = [s for s in (safe_ssid(s) for s in ssids or ()) if s]
        _pool('CLEAR')
        if ssids:
            rc, out, err = _pool('ADD', *ssids, timeout=20)
            if rc != 0:
                logging.warning('could not load the SSID pool: %s',
                                system.first_line(out) or system.first_line(err)
                                or 'rc %d' % rc)
                self.ssids = []
                return False
            loaded = self.advertising()
            if set(loaded) != set(ssids):
                missing = [s for s in ssids if s not in loaded]
                logging.warning('the SSID pool only took %d of %d names '
                                '(rejected: %s)', len(loaded), len(ssids),
                                ', '.join(missing[:3]) or 'unknown')
                if not loaded:
                    self.ssids = []
                    return False
            self.ssids = list(loaded)
            self._requested = list(ssids)
            return True
        self.ssids = []
        return True

    def advertising(self):
        rc, out, _ = _pool('LIST', 'format=json')
        try:
            loaded = json.loads(out)
        except (ValueError, TypeError):
            return _loaded_names(out)
        return [s for s in loaded if isinstance(s, str)] if isinstance(loaded, list) else []

    def _block(self, reason):
        if reason != self.reason:
            logging.info('SSID pool off: %s', reason)
        self.reason = reason
        self.blocked = True
        self._blocked_at = time.monotonic()

    def start(self, ssids, radios=(), channel=0, should_stop=None):
        if not self.enabled or not ssids:
            return False
        if self.blocked:
            if time.monotonic() - self._blocked_at < POOL_RETRY_AFTER:
                return False
            logging.debug('giving the SSID pool another try after %r',
                          self.reason)
            self.blocked = False
        stop = should_stop or (lambda: False)
        with self._lock:
            if not available():
                self._block('SSID pool not supported here')
                return False
            busy = [i for i in radios if i and shares_radio(i)]
            if busy:
                self._block('no spare radio for the SSID pool')
                logging.info('%s on the only radio that could host the SSID '
                             'pool - leaving it off', ', '.join(busy))
                return False
            if stop():
                return False
            if not self._raise_ap(channel, stop):
                self._block('could not bring up the open AP')
                self._lower_ap()
                return False
            if not self._push(ssids):
                self._block('SSID pool would not load')
                self._lower_ap()
                return False
            _pool('BSSIDRANDOM', 'true')
            _pool('RANDOMORDER', 'true')
            rc, out, err = _pool('ENABLE', timeout=20)
            if rc != 0 or 'unable' in (out + err).lower():
                self._block('SSID pool would not start')
                logging.warning('%s: %s', self.reason,
                                system.first_line(out) or system.first_line(err)
                                or 'no reason given')
                self._push([])
                self._lower_ap()
                return False
            self.running = True
            self.reason = ''
            self._disarm_config()
            logging.info('SSID pool broadcasting %d network(s): %s',
                         len(self.ssids), ', '.join(self.ssids[:5]))
            return True

    def refresh(self, ssids):
        if not self.running:
            return False
        with self._lock:
            if not self.running:
                return False
            if set(ssids) == set(self._requested):
                return False
            if not ssids:
                return False
            self._push(ssids)
            system.run_cmd(['_pineap', 'SSIDPOOL', 'RELOAD'], timeout=15)
            logging.info('SSID pool now advertising %d network(s)', len(ssids))
            return True

    def stop(self):
        with self._lock:
            touched = (self.running or self._raised_ap or self._channel_changed
                       or self._touched_config)
            if not touched:
                return
            if self.running:
                _pool('DISABLE', timeout=20)
                self.running = False
            self._push([])
            _uci('-q', 'delete', 'pineapd.@ssidpool[0].ssid')
            _uci('commit', 'pineapd')
            self._lower_ap()
            self.ssids = []
