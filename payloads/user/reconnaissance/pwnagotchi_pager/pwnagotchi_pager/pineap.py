import json
import logging
import re
import subprocess
import threading
import time

from . import config, interfaces, system
from .scope import group_key

PINEAPD_RESTART_EVERY = 120
MAX_PINEAPD_RESTARTS = 3
DEFAULT_AP_TTL = 120
MIN_ACTIVITY_GAP = 5.0
MAX_LEARNED_ESSIDS = 2048
CLIENT_TTL = 300
BROADCAST = 'FF:FF:FF:FF:FF:FF'
DEFAULT_AP_POLL = 8.0


def shell_join(argv):
    out = []
    for a in argv:
        out.append("'" + str(a).replace("'", "'" + chr(92) + "''") + "'")
    return ' '.join(out)


def pineapd_alive():
    rc, out, _ = system.run_cmd(['pidof', 'pineapd'], timeout=5)
    return rc == 0 and bool(out.strip())


OFF_WORDS = ('false', '0', 'no', 'off')


def pineapd_argv():
    rc, out, _ = system.run_cmd(['pidof', 'pineapd'], timeout=5)
    if rc != 0 or not out.strip():
        return []
    try:
        with open('/proc/%s/cmdline' % out.split()[0], 'rb') as fh:
            return fh.read().decode('utf-8', 'replace').split(chr(0))
    except OSError:
        return []


def handshake_logging():
    argv = pineapd_argv()
    if not argv:
        return None
    on = None
    for i, arg in enumerate(argv):
        value = None
        if arg.startswith('--handshakes='):
            value = arg.split('=', 1)[1]
        elif arg == '--handshakes' and i + 1 < len(argv):
            value = argv[i + 1]
        if value is not None:
            on = value.strip().lower() not in OFF_WORDS
    return on


_children = []


def reap_children():
    for proc in list(_children):
        if proc.poll() is not None:
            _children.remove(proc)


class RestartBudget:
    def __init__(self):
        self.used = 0
        self.last = 0.0

    def reset(self):
        self.used = 0

    def may_restart(self):
        if self.used >= MAX_PINEAPD_RESTARTS:
            return False
        return time.monotonic() - self.last >= PINEAPD_RESTART_EVERY

    def defer(self):
        self.last = time.monotonic()

    def spend(self):
        self.used += 1


def relaunch_pineapd():
    reap_children()
    try:
        with open(config.PINEAPD_ARGV) as fh:
            argv = [a for a in fh.read().split(chr(10)) if a.strip()]
    except OSError:
        return False
    if not argv:
        return False
    script = ('ulimit -d 131072 2>/dev/null; exec /usr/sbin/pineapd %s'
              % shell_join(argv))
    try:
        _children.append(subprocess.Popen(['sh', '-c', script],
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL,
                                          stdin=subprocess.DEVNULL,
                                          start_new_session=True))
    except Exception as e:
        logging.warning('could not restart pineapd: %s', e)
        return False
    logging.warning('pineapd had died - restarted it with the payload options')
    return True
DEFAULT_CLIENT_POLL = 24.0
UNKNOWN_RSSI = -100
CLOCK_SLACK = 60
UNRESPONSIVE_AFTER = 3
COMPLAIN_EVERY = 60
MIN_CHANNEL = 1
MAX_CHANNEL = 233

BAND_2G = '2G'
BAND_5G = '5G'
BAND_6G = '6G'
NO_BAND = ''

SENT = 'sent'
REFUSED = 'refused'
FAILED = 'failed'

_MAC_RE = re.compile(r'^[0-9A-F]{2}(:[0-9A-F]{2}){5}$')


def valid_mac(mac):
    return bool(_MAC_RE.match(str(mac or '')))


NOT_A_REFUSAL = ('unable to find requested device', 'invalid bssid',
                 'invalid mac', 'unknown bssid', 'no such device',
                 'not found', 'busy', 'timeout')


REFUSAL_PHRASES = ('operation not permitted', 'not permitted',
                   'not supported', 'unsupported', 'invalid argument',
                   'network is down', 'no ir', 'permission denied',
                   'cannot inject', 'can not inject', 'injection failed',
                   'failed to inject', 'unable to queue inject',
                   'restricted channel', 'radio disabled',
                   'channel not allowed', 'radar', 'dfs',
                   'regulatory', 'tx not allowed')


def _is_refusal(reason):
    text = str(reason or '').strip().lower()
    if not text:
        return False
    if any(phrase in text for phrase in NOT_A_REFUSAL):
        return False
    return any(phrase in text for phrase in REFUSAL_PHRASES)


def freq_to_band_channel(freq):
    freq = _as_int(freq)
    if not freq:
        return NO_BAND, 0
    if 2412 <= freq <= 2472:
        return BAND_2G, (freq - 2407) // 5
    if freq == 2484:
        return BAND_2G, 14
    if 5160 <= freq <= 5895:
        return BAND_5G, (freq - 5000) // 5
    if 5955 <= freq <= 7115:
        return BAND_6G, (freq - 5950) // 5
    return NO_BAND, 0


def valid_channel(channel):
    channel = _as_int(channel)
    return MIN_CHANNEL <= channel <= MAX_CHANNEL


def band_for_channel(channel):
    channel = _as_int(channel)
    if channel <= 0:
        return NO_BAND
    if channel <= 14:
        return BAND_2G
    if channel <= 177:
        return BAND_5G
    if channel <= 233:
        return BAND_6G
    return NO_BAND


BAND_SPEC = {BAND_6G: 'W6e'}


def channel_spec(channel, band=None):
    channel = _as_int(channel)
    if channel <= 0:
        return ''
    return '%d%s' % (channel, BAND_SPEC.get(band or band_for_channel(channel), ''))


def channel_band(channel, band=None):
    return band or band_for_channel(channel)


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _rssi(value):
    level = _as_int(value, UNKNOWN_RSSI)
    return level if level < 0 else UNKNOWN_RSSI


def _when(value, now):
    seen = _as_int(value, 0)
    if not seen or seen > now + CLOCK_SLACK:
        return now
    return seen


def entries(data, key):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        found = data.get(key)
        return found if isinstance(found, list) else []
    return []


def _frames(raw, key):
    group = raw.get(key)
    if not isinstance(group, dict):
        return []
    return [f for f in group.values() if isinstance(f, dict)]


def parse_ap(raw, now):
    if not isinstance(raw, dict):
        return None
    mac = str(raw.get('mac', '')).upper()
    if not valid_mac(mac):
        return None
    beacons = _frames(raw, 'beacon')
    responses = _frames(raw, 'response')
    advertised = ''
    for frame in beacons + responses:
        advertised = advertised or str(frame.get('ssid') or '')
    newest = None
    for frame in beacons + responses:
        if newest is None or _as_int(frame.get('time')) > _as_int(newest.get('time')):
            newest = frame
    channel = _as_int((newest or {}).get('channel'))
    if not valid_channel(channel):
        channel = 0
    if not channel:
        for frame in beacons + responses:
            found = _as_int(frame.get('channel'))
            if valid_channel(found):
                channel = found
                break
    freq = (newest or {}).get('freq') or raw.get('freq', 0)
    freq_band, freq_channel = freq_to_band_channel(freq)
    if freq_channel and freq_channel != channel:
        channel = freq_channel
    seen_at = _when(raw.get('time'), now)
    announced = sum(_as_int(f.get('count')) for f in beacons + responses)
    packets = _as_int(raw.get('packets'))
    return {
        'mac': mac,
        'hostname': advertised,
        'hidden': not advertised,
        'channel': channel,
        'band': freq_band or band_for_channel(channel),
        'rssi': _rssi(raw.get('signal')),
        'packets': packets,
        'activity': max(0, packets - announced),
        'first_seen': seen_at,
        'last_seen': seen_at,
    }


def is_randomised(mac):
    return len(mac) == 17 and mac[1] in '26AEae'


def _station_load(stations):
    load = {}
    for info in stations.values():
        channel = info.get('channel') or info.get('heard_channel')
        band = info.get('band') or info.get('heard_band', NO_BAND)
        if channel:
            key = (channel, band)
            load[key] = load.get(key, 0) + 1
    return load


def typical_load(traffic):
    loads = sorted(v for v in (traffic or {}).values() if v > 0)
    if not loads:
        return 0.0
    middle = len(loads) // 2
    if len(loads) % 2:
        return float(loads[middle])
    return (loads[middle - 1] + loads[middle]) / 2.0


def channel_score(targets, traffic, clients=0, typical=0):
    reach = targets + 2.0 * min(6, clients)
    scale = float(typical) if typical and typical > 0 else 100.0
    return reach * (1.0 + min(2.0, traffic / scale))


def is_group_mac(mac):
    try:
        return bool(int(str(mac)[:2], 16) & 0x01)
    except ValueError:
        return True


def parse_station(raw, now):
    if not isinstance(raw, dict) or 'beacon' in raw or 'response' in raw:
        return None, None
    mac = str(raw.get('mac', '')).upper()
    if not valid_mac(mac) or is_group_mac(mac):
        return None, None
    probes = _frames(raw, 'probe')
    wanted = {}
    for probe in probes:
        ssid = str(probe.get('ssid') or '')
        if not ssid:
            continue
        when = _when(probe.get('time'), now)
        if when > wanted.get(ssid, 0):
            wanted[ssid] = when
    seen_at = _when(raw.get('time'), now)
    band, channel = freq_to_band_channel(raw.get('freq', 0))
    return mac, {
        'ssids': wanted,
        'rssi': _rssi(raw.get('signal')),
        'packets': _as_int(raw.get('packets')),
        'channel': 0 if probes else channel,
        'band': NO_BAND if probes else band,
        'probing': bool(probes),
        'heard_channel': channel,
        'heard_band': band,
        'randomised': is_randomised(mac),
        'last_seen': max([seen_at] + list(wanted.values())),
    }


class PineAP:
    def __init__(self, capture_iface='wlan1mon', ap_ttl=DEFAULT_AP_TTL, keep=None):
        self.capture_iface = capture_iface
        self.keep = [i for i in (keep or []) if i] or [capture_iface]
        self.ap_ttl = ap_ttl
        self.ap_poll = DEFAULT_AP_POLL
        self.client_poll = DEFAULT_CLIENT_POLL
        self.running = False
        self.current_channel = 0
        self.current_band = NO_BAND
        self.focused = None
        self._lock = threading.Lock()
        self._aps = {}
        self._thread = None
        self._stop = threading.Event()
        self._learned_essids = {}
        self._stations = {}
        self._traffic = {}
        self._next_clients = 0.0
        self.recon_failures = 0
        self._lent = False

    def _cmd(self, *args, **kw):
        return system.run_cmd(['_pineap'] + [str(a) for a in args],
                              timeout=kw.get('timeout', 10))

    def interfaces(self):
        rc, out, _ = self._cmd('INTERFACE', 'LIST', 'json')
        names = []
        if out:
            try:
                for entry in json.loads(out):
                    name = entry.get('ifname')
                    if name:
                        names.append(name)
            except (ValueError, AttributeError, TypeError):
                pass
        return names

    def claim_capture_radio(self):
        present = self.interfaces()
        for iface in self.keep:
            if not interfaces.exists(iface):
                logging.warning('%s is not present, not adding it to pineapd',
                                iface)
                continue
            interfaces.ensure_up(iface)
            if iface not in present:
                self._cmd('INTERFACE', 'ADD', iface,
                          'band=%s' % interfaces.bands_for(iface),
                          'type=max', 'rate=fast')
                time.sleep(1)
        present = self.interfaces()
        for iface in present:
            if iface not in self.keep:
                self._cmd('INTERFACE', 'DISABLE', iface)
        for iface in self.keep:
            if iface != self.capture_iface and interfaces.exists(iface):
                self._cmd('INTERFACE', 'ENABLE', iface)
                self._cmd('INTERFACE', 'SET', iface, 'HOP', 'fast')
        self.enable_capture_radio()
        if (self.capture_iface in self.interfaces()
                and interfaces.exists(self.capture_iface)
                and interfaces.is_monitor(self.capture_iface, refresh=True)):
            logging.info('capture radio: %s', self.capture_iface)
            return True
        logging.warning('capture radio %s not active (have %s)',
                        self.capture_iface, present)
        return False

    def enable_capture_radio(self):
        self._lent = False
        self.recon_failures = 0
        interfaces.ensure_up(self.capture_iface)
        self._cmd('INTERFACE', 'ENABLE', self.capture_iface)
        self._cmd('INTERFACE', 'SET', self.capture_iface, 'HOP', 'fast')
        self._cmd('INTERFACE', 'PRIMARY', self.capture_iface)
        self._cmd('INTERFACE', 'INJECT', self.capture_iface)

    def release_capture_radio(self):
        self._lent = True
        self.cancel_examine()
        self._cmd('INTERFACE', 'DISABLE', self.capture_iface)

    def start(self):
        if self.running:
            return
        self.claim_capture_radio()
        self.cancel_examine()
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._recon_loop,
                                        name='recon', daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logging.warning('the recon thread did not stop in time')
        self._thread = None
        self.cancel_examine()

    def _recon_loop(self):
        broken = 0
        while self.running and not self._stop.is_set():
            try:
                self.refresh()
                broken = 0
            except Exception as e:
                broken += 1
                if broken <= 3 or broken % COMPLAIN_EVERY == 0:
                    logging.warning('recon failed (%d in a row): %s', broken, e)
            if time.monotonic() >= self._next_clients:
                self._next_clients = time.monotonic() + self.client_poll
                try:
                    self.refresh_clients()
                except Exception as e:
                    logging.debug('client scan error: %s', e)
                try:
                    self.refresh_traffic()
                except Exception as e:
                    logging.debug('channel scan error: %s', e)
            self._stop.wait(max(1.0, self.ap_poll))

    def sync(self, clients=True):
        try:
            self.refresh()
        except Exception as e:
            logging.debug('sync refresh: %s', e)
        if clients:
            self._next_clients = time.monotonic() + self.client_poll
            try:
                self.refresh_clients()
            except Exception as e:
                logging.debug('sync clients: %s', e)
            try:
                self.refresh_traffic()
            except Exception as e:
                logging.debug('sync channels: %s', e)

    def refresh_clients(self):
        rc, out, _ = self._cmd('RECON', 'DEVICES', 'format=json', 'limit=400',
                               timeout=20)
        if not out:
            return
        try:
            data = json.loads(out)
        except ValueError:
            return
        found_entries = entries(data, 'devices')
        now = time.time()
        found = {}
        for raw in found_entries:
            try:
                mac, info = parse_station(raw, now)
            except Exception as e:
                logging.debug('skipping malformed station: %s', e)
                continue
            if mac and now - info['last_seen'] <= CLIENT_TTL:
                found[mac] = info
        with self._lock:
            for mac, info in found.items():
                self._stations[mac] = info
            self._prune_stations(now)

    def ap_macs(self):
        with self._lock:
            return set(self._aps)

    def client_count(self):
        with self._lock:
            return len(self._stations)

    @property
    def responding(self):
        return self._lent or self.recon_failures < UNRESPONSIVE_AFTER

    def refresh(self):
        rc, out, _ = self._cmd('RECON', 'APS', 'format=json', 'limit=200', timeout=20)
        if rc != 0 or not out:
            if self._lent:
                return
            self.recon_failures += 1
            if self.recon_failures == UNRESPONSIVE_AFTER:
                logging.error('pineapd has stopped answering recon requests')
            return
        try:
            data = json.loads(out)
        except ValueError:
            self.recon_failures += 1
            return
        if self.recon_failures >= UNRESPONSIVE_AFTER:
            logging.warning('pineapd is answering again')
        self.recon_failures = 0
        found_entries = entries(data, 'aps')
        now = time.time()
        seen = {}
        gone = 0
        for raw in found_entries:
            try:
                ap = parse_ap(raw, now)
            except Exception as e:
                logging.debug('skipping malformed AP: %s', e)
                continue
            if ap is None:
                continue
            if now - ap['last_seen'] > self.ap_ttl:
                gone += 1
                continue
            if not ap['hostname']:
                with self._lock:
                    ap['hostname'] = self._learned_essids.get(ap['mac'].lower(), '')
            seen[ap['mac'].lower()] = ap
        if gone:
            logging.debug('%d of %d APs are only pineapd history, not in range',
                          gone, len(found_entries))
        stamp = time.monotonic()
        with self._lock:
            merged = {}
            for key, ap in seen.items():
                prev = self._aps.get(key)
                ap['sampled'] = stamp
                ap['busy'] = 0.0
                if prev:
                    ap['first_seen'] = prev.get('first_seen', now)
                    gap = stamp - prev.get('sampled', stamp)
                    if gap >= MIN_ACTIVITY_GAP:
                        ap['busy'] = max(0, ap.get('activity', 0)
                                         - prev.get('activity', 0)) / gap
                    else:
                        ap['sampled'] = prev.get('sampled', stamp)
                        ap['busy'] = prev.get('busy', 0.0)
                merged[key] = ap
            for key, prev in self._aps.items():
                if key not in merged and now - prev.get('last_seen', 0) < self.ap_ttl:
                    merged[key] = prev
            self._aps = merged

    def learn_essid(self, bssid, essid):
        if not bssid or not essid:
            return
        with self._lock:
            key = bssid.lower()
            if key not in self._learned_essids:
                while len(self._learned_essids) >= MAX_LEARNED_ESSIDS:
                    self._learned_essids.pop(next(iter(self._learned_essids)))
            self._learned_essids[key] = essid

    def stations(self):
        now = time.time()
        with self._lock:
            return {mac: dict(info) for mac, info in self._stations.items()
                    if now - info['last_seen'] <= CLIENT_TTL}

    def access_points(self):
        now = time.time()
        with self._lock:
            aps = [dict(ap) for ap in self._aps.values()]
        live = self.stations()
        load = _station_load(live)

        by_essid = {}
        for ap in aps:
            essid = ap.get('hostname')
            if not essid:
                continue
            by_essid.setdefault(essid, []).append(ap)
        for group in by_essid.values():
            group.sort(key=lambda a: -a.get('rssi', UNKNOWN_RSSI))

        bound = {}
        for mac, info in live.items():
            where = (info.get('channel') or info.get('heard_channel'),
                     info.get('band') or info.get('heard_band', NO_BAND))
            for essid, when in info['ssids'].items():
                if now - when > CLIENT_TTL:
                    continue
                group = by_essid.get(essid)
                if not group:
                    continue
                owner = None
                if where[0]:
                    for ap in group:
                        if (ap.get('channel'), ap.get('band', NO_BAND)) == where:
                            owner = ap
                            break
                if owner is None:
                    owner = group[0]
                bound.setdefault(owner['mac'], set()).add(mac)

        claimed = set()
        for group in bound.values():
            claimed |= group
        family = {}
        for ap in aps:
            key = group_key(ap['mac'])
            if key:
                family.setdefault(key, set()).update(bound.get(ap['mac'], ()))
        nearby = {}
        for mac, info in live.items():
            if mac in claimed:
                continue
            key = (info.get('channel') or info.get('heard_channel'),
                   info.get('band') or info.get('heard_band', NO_BAND))
            if not key[0]:
                continue
            nearby.setdefault(key, []).append((info.get('rssi', UNKNOWN_RSSI), mac))

        for ap in aps:
            ap['clients'] = sorted(bound.get(ap['mac'], ()))
            key = (ap.get('channel'), ap.get('band', NO_BAND))
            ap['channel_clients'] = load.get(key, 0)
            ap['nearby'] = tuple(m for _, m in
                                 sorted(nearby.get(key, ()), reverse=True))
            relatives = family.get(group_key(ap['mac']), set())
            ap['kin'] = tuple(sorted(relatives - set(ap['clients'])))
            ap['heard'] = {mac: live[mac].get('rssi', UNKNOWN_RSSI)
                           for mac in set(ap['clients']).union(ap['kin'],
                                                               ap['nearby'])
                           if mac in live}
        return aps

    def clear(self):
        with self._lock:
            self._aps = {}

    def _prune_stations(self, now):
        for mac in list(self._stations):
            if now - self._stations[mac]['last_seen'] > CLIENT_TTL:
                del self._stations[mac]

    def refresh_traffic(self):
        rc, out, _ = self._cmd('RECON', 'CHANNELS', 'format=json', timeout=10)
        if not out:
            return
        try:
            data = json.loads(out)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        load = {}
        for freq, count in data.items():
            band, channel = freq_to_band_channel(freq)
            if channel:
                key = (channel, band)
                load[key] = load.get(key, 0) + _as_int(count)
        with self._lock:
            self._traffic = load

    def traffic(self):
        with self._lock:
            return dict(self._traffic)

    def by_channel(self, aps, only=None, weigh=None):
        grouped = {}
        for ap in aps:
            ch = ap.get('channel', 0)
            if not ch or (only and ch not in only):
                continue
            grouped.setdefault((ch, ap.get('band', NO_BAND)), []).append(ap)
        traffic = self.traffic()
        typical = typical_load(traffic)

        def rank(kv):
            key, group = kv
            clients = sum(len(ap.get('clients') or []) for ap in group)
            clients += max([ap.get('channel_clients', 0) for ap in group] or [0])
            score = channel_score(len(group), traffic.get(key, 0), clients,
                                  typical)
            if weigh is not None:
                try:
                    score *= float(weigh(key[0], key[1]))
                except Exception:
                    pass
            return (-score, key[0], key[1])

        return sorted(grouped.items(), key=rank)

    def examine_channel(self, channel, seconds, band=None):
        if not valid_channel(channel) or seconds <= 0:
            return False
        band = band or band_for_channel(channel)
        spec = channel_spec(channel, band)
        if not spec:
            return False
        self.current_channel = channel
        self.current_band = band
        rc, out, err = self._cmd('EXAMINE', 'CHANNEL', spec, int(seconds))
        if rc != 0:
            logging.debug('examine channel %s: %s', spec,
                          system.first_line(out) or system.first_line(err))
        return rc == 0

    def examine_bssid(self, bssid, seconds):
        if not valid_mac(str(bssid or '').upper()) or seconds <= 0:
            return False
        self.focused = bssid
        rc, out, err = self._cmd('EXAMINE', 'BSSID', bssid, int(seconds))
        if rc != 0:
            logging.debug('examine bssid %s: %s', bssid,
                          system.first_line(out) or system.first_line(err))
        return rc == 0

    def cancel_examine(self):
        self.current_channel = 0
        self.current_band = NO_BAND
        self.focused = None
        self._cmd('EXAMINE', 'CANCEL')

    def deauth(self, bssid, target=BROADCAST, channel=None, bursts=1, gap=0.15,
               should_stop=None):
        if not valid_mac(str(bssid or '').upper()):
            return FAILED
        with self._lock:
            known = self._aps.get(bssid.lower())
        if channel is None:
            channel = known.get('channel', 0) if known else self.current_channel
        if known and known.get('band') == BAND_6G:
            logging.debug('not deauthing %s: a 6 GHz channel cannot be aimed '
                          'through this command', bssid)
            return FAILED
        if not valid_channel(channel):
            return FAILED
        outcome = FAILED
        for i in range(max(1, bursts)):
            rc, out, err = self._cmd('DEAUTH', bssid, target, int(channel))
            if rc == 0:
                outcome = SENT
            elif rc > 0 and outcome != SENT:
                reason = system.first_line(out) or system.first_line(err)
                if _is_refusal(reason):
                    outcome = REFUSED
                else:
                    logging.debug('deauth %s: %s', bssid, reason or 'rc %d' % rc)
            if should_stop and should_stop():
                break
            if i + 1 < bursts and gap > 0:
                time.sleep(gap)
        return outcome
