import json
import logging
import os
import threading
import time

DEAUTH = 'deauth'
PMKID = 'pmkid'
PINEAP = 'pineap'
KINDS = (DEAUTH, PMKID, PINEAP)
MAX_CHANNELS = 64
CONFIDENCE = 3.0
SAVE_EVERY = 60.0
VERSION = 1
SIXTH = '6G'


def key(channel, band=None):
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return ''
    if channel <= 0:
        return ''
    if band == SIXTH or (not band and channel > 177):
        return '%d %s' % (channel, SIXTH)
    return '%d' % channel


def _num(value, default=0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if value != value or value in (float('inf'), float('-inf')):
        return default
    return value


class Yield:
    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        self._writing = threading.Lock()
        self.started_at = time.monotonic()
        self.paused_for = 0.0
        self.captures = dict.fromkeys(KINDS, 0)
        self.dry = dict.fromkeys(KINDS, 0)
        self.sweeps = 0
        self.attempts = 0
        self.airtime = 0.0
        self.channels = {}
        self.lifetime = dict.fromkeys(KINDS, 0)
        self.lifetime.update({'attempts': 0, 'airtime': 0.0,
                              'runtime': 0.0, 'runs': 0})
        self._saved_at = 0.0
        self.load()

    def _slot(self, channel):
        row = self.channels.get(channel)
        if row is None:
            if len(self.channels) >= MAX_CHANNELS:
                weakest = min(self.channels,
                              key=lambda c: self.channels[c]['attempts'])
                del self.channels[weakest]
            row = {'attempts': 0, 'captures': 0, 'airtime': 0.0}
            self.channels[channel] = row
        return row

    def attempt(self, channel, band=None):
        slot = key(channel, band)
        with self._lock:
            self.dry[DEAUTH] += 1
            if slot:
                self.attempts += 1
                self._slot(slot)['attempts'] += 1

    def swept(self):
        with self._lock:
            self.sweeps += 1
            self.dry[PMKID] += 1

    def spent(self, channel, seconds, band=None):
        seconds = _num(seconds)
        slot = key(channel, band)
        if not slot or seconds <= 0:
            return
        with self._lock:
            self.airtime += seconds
            self._slot(slot)['airtime'] += seconds

    def captured(self, channel, how=DEAUTH, count=1, band=None):
        if how not in self.captures:
            how = DEAUTH
        slot = key(channel, band)
        with self._lock:
            self.captures[how] += count
            self.dry[how] = 0
            if slot:
                self._slot(slot)['captures'] += count

    @property
    def total(self):
        return sum(self.captures[k] for k in KINDS)

    def idled(self, seconds):
        if seconds > 0:
            with self._lock:
                self.paused_for += seconds

    def runtime(self):
        return max(0.0, time.monotonic() - self.started_at - self.paused_for)

    def per_hour(self, lifetime=False):
        if lifetime:
            hours = (self.lifetime['runtime'] + self.runtime()) / 3600.0
            got = sum(self.lifetime[k] for k in KINDS) + self.total
        else:
            hours = self.runtime() / 3600.0
            got = self.total
        return got / hours if hours > 0.02 else 0.0

    def cost(self):
        return self.airtime / self.total if self.total else 0.0

    def ranked(self, limit=6):
        with self._lock:
            rows = [(c, dict(r)) for c, r in self.channels.items()]
        for channel, row in rows:
            row['channel'] = channel
            row['rate'] = (row['captures'] / row['airtime'] * 3600.0
                           if row['airtime'] > 1 else 0.0)
        rows.sort(key=lambda cr: (-cr[1]['rate'], -cr[1]['captures'], cr[0]))
        return [row for _, row in rows[:limit]]

    def bias(self, channel, band=None, floor=0.5, ceiling=2.0):
        slot = key(channel, band)
        if not slot:
            return 1.0
        with self._lock:
            row = self.channels.get(slot)
            if not row:
                return 1.0
            total_air = sum(r['airtime'] for r in self.channels.values())
            total_cap = sum(r['captures'] for r in self.channels.values())
            mine_air = row['airtime']
            mine_cap = row['captures']
        if total_air <= 0 or total_cap <= 0 or mine_air <= 0:
            return 1.0
        expected = (total_cap / total_air) * mine_air
        if expected <= 0:
            return 1.0
        trust = expected / (expected + CONFIDENCE)
        return max(floor, min(ceiling,
                              1.0 + (mine_cap / expected - 1.0) * trust))

    def barren(self, min_airtime=120.0):
        with self._lock:
            return sorted(c for c, r in self.channels.items()
                          if not r['captures'] and r['airtime'] >= min_airtime)

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as fh:
                stored = json.load(fh)
        except Exception as e:
            logging.debug('yield history unreadable: %s', e)
            return
        if not isinstance(stored, dict) or stored.get('version') != VERSION:
            return
        totals = stored.get('lifetime')
        if isinstance(totals, dict):
            for name in self.lifetime:
                value = _num(totals.get(name), 0)
                self.lifetime[name] = value if name in ('airtime', 'runtime') \
                    else int(value)
        channels = stored.get('channels')
        if isinstance(channels, dict):
            for slot, row in list(channels.items())[:MAX_CHANNELS]:
                slot = str(slot).strip()
                if not slot or not isinstance(row, dict):
                    continue
                if not slot.split(' ')[0].isdigit():
                    continue
                self.channels[slot] = {
                    'attempts': int(_num(row.get('attempts'))),
                    'captures': int(_num(row.get('captures'))),
                    'airtime': _num(row.get('airtime')),
                }

    def save(self, force=False):
        if not self.path:
            return
        now = time.monotonic()
        if not force and now - self._saved_at < SAVE_EVERY:
            return
        with self._lock:
            payload = {
                'version': VERSION,
                'lifetime': {
                    DEAUTH: self.lifetime[DEAUTH] + self.captures[DEAUTH],
                    PMKID: self.lifetime[PMKID] + self.captures[PMKID],
                    PINEAP: self.lifetime[PINEAP] + self.captures[PINEAP],
                    'attempts': self.lifetime['attempts'] + self.attempts,
                    'airtime': self.lifetime['airtime'] + self.airtime,
                    'runtime': self.lifetime['runtime'] + self.runtime(),
                    'runs': self.lifetime['runs'] + 1,
                },
                'channels': {str(c): dict(r)
                             for c, r in self.channels.items()},
            }
            self._saved_at = now
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = '%s.%d.tmp' % (self.path, os.getpid())
            with self._writing:
                with open(tmp, 'w') as fh:
                    json.dump(payload, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
        except Exception as e:
            logging.debug('could not save yield history: %s', e)
            with self._lock:
                self._saved_at = 0.0
