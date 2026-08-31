import logging
import time

CAMPED = 'camped'
DRIFTING = 'drifting'
ROAMING = 'roaming'


PROFILES = {
    CAMPED: {
        'recon_scale': 1.0,
        'dwell_scale': 1.4,
        'ap_ttl': 180,
        'retune_after': 90,
        'ap_poll': 10.0,
        'client_poll': 30.0,
        'label': '',
    },
    DRIFTING: {
        'recon_scale': 0.75,
        'dwell_scale': 1.0,
        'ap_ttl': 90,
        'retune_after': 45,
        'ap_poll': 6.0,
        'client_poll': 20.0,
        'label': 'moving',
    },
    ROAMING: {
        'recon_scale': 0.5,
        'dwell_scale': 0.6,
        'ap_ttl': 45,
        'retune_after': 20,
        'ap_poll': 4.0,
        'client_poll': 12.0,
        'label': 'moving fast',
    },
}


DRIFT_AT = 0.15
ROAM_AT = 0.35
SMOOTHING = 0.6
MIN_SAMPLE = 4
STABLE_RSSI = -75


def _heard_well(ap):
    if not isinstance(ap, dict) or not ap.get('mac'):
        return False
    try:
        return float(ap.get('rssi', -100)) >= STABLE_RSSI
    except (TypeError, ValueError):
        return False


class Mobility:
    def __init__(self):
        self.state = CAMPED
        self.churn = 0.0
        self._seen = set()
        self._seeded = False
        self._disturbed = False
        self._changed_at = time.monotonic()

    def observe(self, aps):
        strong = {ap['mac'].lower() for ap in aps or () if _heard_well(ap)}
        if len(strong) >= MIN_SAMPLE:
            current = strong
        else:
            current = {ap['mac'].lower() for ap in aps or ()
                       if isinstance(ap, dict) and ap.get('mac')}
        if not self._seeded:
            self._seeded = True
            self._seen = current
            return self.state
        disturbed, self._disturbed = self._disturbed, False
        thin = len(current) < MIN_SAMPLE
        shrank = disturbed and len(current) < len(self._seen)
        union = self._seen | current
        if thin or shrank or len(union) < MIN_SAMPLE:
            self.churn *= SMOOTHING
            self._seen = current or self._seen
            return self._settle()
        changed = len(self._seen ^ current)
        churn = changed / float(len(union))
        self.churn = SMOOTHING * self.churn + (1.0 - SMOOTHING) * churn
        self._seen = current
        return self._settle()

    def _settle(self):
        if self.churn >= ROAM_AT:
            state = ROAMING
        elif self.churn >= DRIFT_AT:
            state = DRIFTING
        else:
            state = CAMPED
        if state != self.state:
            self.state = state
            self._changed_at = time.monotonic()
        return self.state

    def rebaseline(self):
        self._disturbed = True

    @property
    def profile(self):
        return PROFILES[self.state]

    @property
    def moving(self):
        return self.state != CAMPED

    def scale(self, key, value):
        return value * self.profile[key]

    def describe(self):
        return self.profile['label']

    def detail(self):
        return '%s churn=%.0f%%' % (self.state, self.churn * 100)


QUIET = 'quiet'
BUSY = 'busy'
LOUD = 'loud'
THIN = 'thin'
STEADY = 'steady'
CROWDED = 'crowded'
NEAR = 'near'
MIXED = 'mixed'
FAR = 'far'
UNHEARD = -100
RX_SAMPLE = '/sys/class/net/%s/statistics/rx_packets'
DEAF_EPOCHS = 3
MIN_HEARD = 20


def median(values):
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def rx_packets(iface):
    try:
        with open(RX_SAMPLE % iface) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


class Environment:
    def __init__(self, settings):
        self.settings = settings
        self.density = STEADY
        self.reach = MIXED
        self.targets = 0
        self.clients = 0
        self.median_rssi = None
        self.best_rssi = None
        self._traffic = {}
        self._typical = 0.0
        self._rx = {}
        self._peak = {}
        self._deaf = {}
        self.deaf_radios = ()

    def tune(self, key):
        return self.settings.tune(key)

    def observe(self, targetable, stations, traffic):
        self.targets = len(targetable or ())
        self.clients = len(stations or ())
        self._traffic = dict(traffic or {})
        self._typical = median([v for v in self._traffic.values() if v]) or 0.0

        levels = [ap.get('rssi', UNHEARD) for ap in (targetable or ())]
        self.median_rssi = median(levels)
        self.best_rssi = max(levels) if levels else None

        thin = self.tune('rssi_rich_targets')
        crowded = self.tune('crowded_targets')
        if self.targets <= max(1, thin - 1):
            self.density = THIN
        elif self.targets >= crowded:
            self.density = CROWDED
        else:
            self.density = STEADY

        if self.median_rssi is None:
            self.reach = MIXED
        elif self.median_rssi >= self.tune('near_rssi'):
            self.reach = NEAR
        elif self.median_rssi <= self.tune('handshake_rssi'):
            self.reach = FAR
        else:
            self.reach = MIXED

    def noise(self, channel, band=None):
        if not self._typical:
            return QUIET
        load = self._traffic.get((channel, band), 0)
        if load >= self._typical * 3:
            return LOUD
        if load >= self._typical:
            return BUSY
        return QUIET

    def crowded_channel(self, channel, band=None):
        return self.noise(channel, band) in (BUSY, LOUD)

    def sample_radios(self, radios):
        watching = [i for i in (radios or ()) if i]
        for iface in watching:
            count = rx_packets(iface)
            if count is None:
                self._forget_radio(iface)
                continue
            was = self._rx.get(iface)
            self._rx[iface] = count
            if was is None or count < was:
                self._deaf[iface] = 0
                continue
            heard = count - was
            peak = max(self._peak.get(iface, 0), heard)
            self._peak[iface] = peak
            if heard or peak < MIN_HEARD:
                self._deaf[iface] = 0
            else:
                self._deaf[iface] = self._deaf.get(iface, 0) + 1
        for iface in list(self._deaf):
            if iface not in watching:
                self._forget_radio(iface)
        fresh = tuple(sorted(i for i, missed in self._deaf.items()
                             if missed >= DEAF_EPOCHS))
        if fresh != self.deaf_radios:
            for iface in fresh:
                if iface not in self.deaf_radios:
                    logging.warning('%s has heard nothing at all for %d epochs, '
                                    'having managed %d frames a turn before - '
                                    'its radio looks wedged', iface,
                                    DEAF_EPOCHS, self._peak.get(iface, 0))
            self.deaf_radios = fresh

    def _forget_radio(self, iface):
        self._rx.pop(iface, None)
        self._peak.pop(iface, None)
        self._deaf.pop(iface, None)

    def describe(self):
        parts = []
        if self.density != STEADY:
            parts.append(self.density)
        if self.reach != MIXED:
            parts.append(self.reach)
        return ' '.join(parts)

    def detail(self):
        return ('%s/%s targets=%d clients=%d median=%s typical_load=%.0f'
                % (self.density, self.reach, self.targets, self.clients,
                   '--' if self.median_rssi is None else '%.0f' % self.median_rssi,
                   self._typical))
