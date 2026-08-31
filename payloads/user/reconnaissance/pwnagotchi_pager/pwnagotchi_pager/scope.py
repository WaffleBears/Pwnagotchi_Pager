import threading


SIBLING_PREFIX = 14
LOCAL_BIT = 0x02
MAX_LEARNED = 512


def _norm(value):
    if not value:
        return ''
    return str(value).strip().lower()


def group_key(bssid):
    bssid = _norm(bssid)
    if len(bssid) != 17:
        return ''
    try:
        first = int(bssid[:2], 16) & ~LOCAL_BIT
    except ValueError:
        return ''
    return '%02x%s' % (first, bssid[2:SIBLING_PREFIX])


def _remember(store, key):
    if key in store:
        return
    while len(store) >= MAX_LEARNED:
        store.pop(next(iter(store)))
    store[key] = True


def _same_radio_group(a, b):
    key = group_key(a)
    return bool(key) and key == group_key(b)


def normalise_entries(entries):
    out = []
    if not isinstance(entries, (list, tuple, set)):
        entries = []
    for entry in entries or []:
        if isinstance(entry, str):
            entry = {'ssid': entry, 'bssid': ''}
        if not isinstance(entry, dict):
            continue
        ssid = str(entry.get('ssid') or '').strip()
        bssid = str(entry.get('bssid') or '').strip()
        if ssid or bssid:
            out.append({'ssid': ssid, 'bssid': bssid})
    return out


def entry_matches(entry, ssid, bssid, cover_siblings=True, strict=False):
    e_ssid = _norm(entry.get('ssid'))
    e_bssid = _norm(entry.get('bssid'))
    ssid, bssid = _norm(ssid), _norm(bssid)
    if e_bssid and bssid:
        if e_bssid == bssid:
            return True
        if cover_siblings and _same_radio_group(e_bssid, bssid):
            return True
    if strict and e_bssid:
        return False
    return bool(e_ssid and ssid and e_ssid == ssid)


def listed(entries, ssid, bssid, cover_siblings=True, strict=False):
    for entry in entries:
        if entry_matches(entry, ssid, bssid, cover_siblings, strict):
            return True
    return False


def contains(entries, needle):
    needle = _norm(needle)
    if not needle:
        return False
    for entry in entries:
        if _norm(entry.get('ssid')) == needle or _norm(entry.get('bssid')) == needle:
            return True
    return False


class Scope:
    def __init__(self, captures, whitelist=None, blacklist=None,
                 skip_captured=True, cover_siblings=True, max_interactions=3):
        self.captures = captures
        self.whitelist = normalise_entries(whitelist)
        self.blacklist = normalise_entries(blacklist)
        self.skip_captured = skip_captured
        self.cover_siblings = cover_siblings
        self.max_interactions = max_interactions
        self._lock = threading.RLock()
        self._history = {}
        self._white_groups = {}
        self._white_bssids = {}
        self._black_groups = set()
        self._white_ssids = set()
        self._seen = ()

    def update(self, whitelist=None, blacklist=None, skip_captured=None,
               cover_siblings=None):
        with self._lock:
            if whitelist is not None:
                fresh = normalise_entries(whitelist)
                if fresh != self.whitelist:
                    self._white_groups = {}
                    self._white_bssids = {}
                self.whitelist = fresh
            if blacklist is not None:
                self.blacklist = normalise_entries(blacklist)
            if skip_captured is not None:
                self.skip_captured = skip_captured
            if cover_siblings is not None:
                if cover_siblings != self.cover_siblings:
                    self._white_groups = {}
                    self._white_bssids = {}
                self.cover_siblings = cover_siblings
            self._resolve()

    def observe(self, aps):
        seen = tuple((_norm(ap.get('hostname')), _norm(ap.get('mac')))
                     for ap in aps or ())
        with self._lock:
            self._seen = seen
            self._resolve()

    def _resolve(self):
        seen = self._seen
        self._black_groups = self._groups_for(self.blacklist, seen,
                                             strict=True)
        if not self.whitelist:
            self._white_groups = {}
            self._white_bssids = {}
            self._white_ssids = set()
            return
        for essid, bssid in seen:
            if listed(self.whitelist, essid, bssid, self.cover_siblings):
                self._learn_white(bssid)
        for essid, bssid in seen:
            if self._covered(self._white_groups, bssid):
                self._learn_white(bssid)
        self._white_ssids = {essid for essid, bssid in seen
                             if essid and self._listed_white(essid, bssid)}

    def _learn_white(self, bssid):
        bssid = _norm(bssid)
        if len(bssid) != 17:
            return
        _remember(self._white_bssids, bssid)
        if not self.cover_siblings:
            return
        key = group_key(bssid)
        if key:
            _remember(self._white_groups, key)

    def _groups_for(self, entries, seen, strict=False):
        if not entries or not self.cover_siblings:
            return set()
        groups = set()
        for essid, bssid in seen:
            key = group_key(bssid)
            if key and listed(entries, essid, bssid, True, strict):
                groups.add(key)
        return groups

    def _covered(self, groups, bssid):
        if not groups:
            return False
        key = group_key(bssid)
        return bool(key) and key in groups

    def _listed_white(self, ssid, bssid):
        if listed(self.whitelist, ssid, bssid, self.cover_siblings):
            return True
        if _norm(bssid) in self._white_bssids:
            return True
        return self._covered(self._white_groups, bssid)

    def _listed_black(self, ssid, bssid):
        return (listed(self.blacklist, ssid, bssid, self.cover_siblings,
                       strict=True)
                or self._covered(self._black_groups, bssid))

    @property
    def restricted(self):
        return bool(self.whitelist or self.blacklist)

    def in_scope(self, ap):
        ssid, bssid = ap.get('hostname', ''), ap.get('mac', '')
        with self._lock:
            if self._listed_white(ssid, bssid):
                return False
            if self.blacklist:
                return self._listed_black(ssid, bssid)
            return True

    def captured(self, ap):
        return self.captures.holds(ap.get('mac', ''), ap.get('hostname', ''),
                                   self.cover_siblings)

    def targetable(self, ap):
        if not self.in_scope(ap):
            return False
        if self.skip_captured and self.captured(ap):
            return False
        return True

    def _key(self, ap, station=None):
        return (station or ap.get('mac', '')).lower()

    def may_attack(self, ap, station=None):
        if not self.targetable(ap):
            return False
        with self._lock:
            return (self._history.get(self._key(ap, station), 0)
                    < self.max_interactions)

    def record_attack(self, ap, station=None):
        key = self._key(ap, station)
        with self._lock:
            self._history[key] = self._history.get(key, 0) + 1

    def ssid_allowed(self, ssid):
        ssid = (ssid or '').strip()
        if not ssid:
            return False
        with self._lock:
            return self._ssid_allowed(ssid)

    def _ssid_allowed(self, ssid):
        if _norm(ssid) in self._white_ssids:
            return False
        if listed(self.whitelist, ssid, '', self.cover_siblings):
            return False
        if self.blacklist and not listed(self.blacklist, ssid, '',
                                         self.cover_siblings):
            return False
        if self.skip_captured and self.captures.holds_essid(ssid):
            return False
        return True

    def reset_interactions(self):
        with self._lock:
            self._history = {}

    def pmkid_targets(self, aps):
        if not self.restricted and not self.skip_captured:
            return None
        return {ap.get('mac', '').lower() for ap in (aps or [])
                if ap.get('mac') and self.targetable(ap)}
