import logging
import os
import time

from . import system

BUILTIN = 'wlan1mon'
MK7AC = 'wlan2mon'
SECONDARY = 'wlan0mon'

AUTO = 'auto'
BUILTIN_ONLY = 'builtin'
MK7AC_FIRST = 'mk7ac'
CHOICES = (AUTO, BUILTIN_ONLY, MK7AC_FIRST)

ABSENT = 'absent'
NOT_MONITOR = 'not-monitor'
READY = 'ready'

_CACHE_TTL = 10.0
_cache = {'value': None, 'at': 0.0}


def exists(iface):
    return os.path.exists('/sys/class/net/%s' % iface)


def is_up(iface):
    try:
        with open('/sys/class/net/%s/flags' % iface) as f:
            return bool(int(f.read().strip(), 16) & 1)
    except (OSError, ValueError):
        return False


def ensure_up(iface):
    if not iface or not exists(iface):
        return False
    if is_up(iface):
        return True
    rc, _, err = system.run_cmd(['ip', 'link', 'set', iface, 'up'], timeout=10)
    if rc != 0:
        logging.warning('could not bring %s up: %s', iface,
                        system.first_line(err) or 'rc %d' % rc)
        return False
    logging.info('%s was down, brought it up', iface)
    return True


_monitor_cache = {}


def is_monitor(iface, refresh=False):
    now = time.monotonic()
    cached = _monitor_cache.get(iface)
    if not refresh and cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]
    rc, out, _ = system.run_cmd(['iw', 'dev', iface, 'info'], timeout=5)
    value = rc == 0 and 'type monitor' in out
    _monitor_cache[iface] = (value, now)
    return value


def forget_monitor_cache():
    _monitor_cache.clear()
    _bands_cache.clear()
    _phy_cache.clear()
    _pinned_cache.clear()
    _cache['value'] = None
    _cache['at'] = 0.0


def phys():
    try:
        return sorted(os.listdir('/sys/class/ieee80211'))
    except OSError:
        return []


def ifaces_on(phy):
    try:
        return sorted(os.listdir('/sys/class/ieee80211/%s/device/net' % phy))
    except OSError:
        return []


def spare_phys(claimed=()):
    used = {phy_of(iface) for iface in claimed if iface}
    used.discard('')
    return [phy for phy in phys() if phy not in used]


def set_monitor(iface):
    if not exists(iface):
        return False
    system.run_cmd(['ip', 'link', 'set', iface, 'down'], timeout=10)
    rc, _, err = system.run_cmd(['iw', 'dev', iface, 'set', 'type', 'monitor'],
                                timeout=10)
    system.run_cmd(['ip', 'link', 'set', iface, 'up'], timeout=10)
    if rc != 0:
        logging.warning('could not put %s into monitor mode: %s', iface,
                        system.first_line(err) or 'rc %d' % rc)
        return False
    return is_monitor(iface, refresh=True)


PINNING_MODES = ('managed', 'AP', 'AP/VLAN', 'mesh point', 'IBSS',
                 'P2P-client', 'P2P-GO')
IDLE_MODES = ('managed',)

_pinned_cache = {}
_released = set()


def vif_type(iface):
    rc, out, _ = system.run_cmd(['iw', 'dev', iface, 'info'], timeout=5)
    if rc != 0:
        return ''
    for line in out.splitlines():
        text = line.strip()
        if text.startswith('type '):
            return text[5:].strip()
    return ''


def carrying(iface):
    try:
        with open('/sys/class/net/%s/operstate' % iface) as fh:
            return fh.read().strip() == 'up'
    except OSError:
        return False


def free_phy(iface):
    phy = phy_of(iface)
    if not phy:
        return False
    freed = False
    for other in ifaces_on(phy):
        if other == iface or not is_up(other):
            continue
        if vif_type(other) not in IDLE_MODES or carrying(other):
            continue
        rc, _, err = system.run_cmd(['ip', 'link', 'set', other, 'down'],
                                    timeout=10)
        if rc != 0:
            logging.warning('could not take %s down to free %s: %s', other,
                            phy, system.first_line(err) or 'rc %d' % rc)
            continue
        logging.warning('%s was idle but holding %s - took it down so %s can '
                        'tune', other, phy, iface)
        _released.add(other)
        freed = True
    if freed:
        _pinned_cache.pop(iface, None)
    return freed


def restore_released():
    for other in sorted(_released):
        if exists(other) and not is_up(other):
            system.run_cmd(['ip', 'link', 'set', other, 'up'], timeout=10)
            logging.info('put %s back up', other)
    _released.clear()


def phy_pinned_by(iface, refresh=False):
    phy = phy_of(iface)
    if not phy:
        return ()
    now = time.monotonic()
    cached = _pinned_cache.get(iface)
    if not refresh and cached and now - cached[1] < _CACHE_TTL:
        return cached[0]
    holders = tuple(sorted(
        other for other in ifaces_on(phy)
        if other != iface and is_up(other) and vif_type(other) in PINNING_MODES))
    if cached is None or holders != cached[0]:
        if holders:
            logging.warning('%s cannot hold a channel of its own: %s already '
                            'has %s', iface, ', '.join(holders), phy)
        elif cached is not None:
            logging.info('%s is free to tune again', iface)
    _pinned_cache[iface] = (holders, now)
    return holders


def is_usable(iface, refresh=False):
    return (bool(iface) and exists(iface) and is_monitor(iface, refresh=refresh)
            and not phy_pinned_by(iface, refresh=refresh))


def _first_monitor(candidates, refresh=False):
    for iface in candidates:
        if is_usable(iface, refresh):
            return iface
    return None


_promote = {'at': 0.0}
_PROMOTE_COOLDOWN = 30.0
KNOWN_RADIOS = (BUILTIN, SECONDARY, 'wlan0', 'wlan1')


def promote_mk7ac():
    now = time.monotonic()
    if now - _promote['at'] < _PROMOTE_COOLDOWN:
        return False
    _promote['at'] = now
    if exists(MK7AC):
        if is_monitor(MK7AC, refresh=True):
            return True
        logging.info('%s is present but not in monitor mode, converting it',
                     MK7AC)
        return set_monitor(MK7AC)
    spare = spare_phys(KNOWN_RADIOS + (MK7AC,))
    if not spare:
        return False
    phy = spare[0]
    logging.info('unclaimed radio %s appeared, bringing up %s on it', phy, MK7AC)
    add = ['iw', 'phy', phy, 'interface', 'add', MK7AC, 'type', 'monitor']
    rc, _, err = system.run_cmd(add, timeout=15)
    lowered = []
    if rc != 0:
        others = [i for i in ifaces_on(phy) if i != MK7AC and is_up(i)]
        if not others:
            logging.warning('could not add %s on %s: %s', MK7AC, phy,
                            system.first_line(err) or 'rc %d' % rc)
            return False
        logging.info('%s is busy with %s, taking it down first',
                     phy, ', '.join(others))
        for iface in others:
            system.run_cmd(['ip', 'link', 'set', iface, 'down'], timeout=10)
        lowered = others
        rc, _, err = system.run_cmd(add, timeout=15)
    if rc != 0:
        for iface in lowered:
            logging.info('putting %s back up', iface)
            system.run_cmd(['ip', 'link', 'set', iface, 'up'], timeout=10)
        logging.warning('could not add %s on %s: %s', MK7AC, phy,
                        system.first_line(err) or 'rc %d' % rc)
        return False
    ensure_up(MK7AC)
    _monitor_cache.pop(MK7AC, None)
    _bands_cache.pop(MK7AC, None)
    _phy_cache.pop(phy, None)
    return is_monitor(MK7AC, refresh=True)


def mk7ac_status(refresh=False, promote=False):
    now = time.monotonic()
    if not refresh and _cache['value'] and (now - _cache['at']) < _CACHE_TTL:
        return _cache['value']
    if promote and not (exists(MK7AC) and is_monitor(MK7AC)):
        promote_mk7ac()
    if not exists(MK7AC):
        value = ABSENT
    elif not is_monitor(MK7AC, refresh=refresh):
        value = NOT_MONITOR
    else:
        value = READY
    if value != _cache['value'] and _cache['value'] is not None:
        logging.warning('MK7AC radio went %s -> %s', _cache['value'], value)
    _cache['value'] = value
    _cache['at'] = now
    return value


_regdomain = {'value': None, 'at': 0.0}
_REGDOMAIN_TTL = 60.0


def regdomain(refresh=False):
    now = time.monotonic()
    if (not refresh and _regdomain['value']
            and now - _regdomain['at'] < _REGDOMAIN_TTL):
        return _regdomain['value']
    rc, out, _ = system.run_cmd(['iw', 'reg', 'get'], timeout=5)
    code = ''
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('country '):
                code = line.split()[1].rstrip(':')
                break
    if not code:
        return _regdomain['value'] or ''
    _regdomain['value'] = code
    _regdomain['at'] = now
    return code


_bands_cache = {}
_BAND_INDEX = {'1': '2', '2': '5', '4': '6'}
RX = 'rx'
TX = 'tx'


def phy_of(iface):
    path = '/sys/class/net/%s/phy80211' % iface
    if not iface or not os.path.exists(path):
        return ''
    try:
        return os.path.basename(os.path.realpath(path))
    except OSError:
        return ''


_phy_cache = {}
_PHY_TTL = 120.0
_PHY_RETRY = 5.0
_UNUSABLE = 'disabled'
_NO_TRANSMIT = ('disabled', 'no ir', 'radar detection')


def _phy_survey(phy, refresh=False):
    cached = _phy_cache.get(phy)
    if not refresh and cached and (time.monotonic() - cached[1]) < cached[2]:
        return cached[0]
    rc, out, _ = system.run_cmd(['iw', 'phy', phy, 'info'], timeout=20)
    survey = {RX: {}, TX: {}}
    if rc == 0 and out:
        band = None
        for line in out.splitlines():
            text = line.strip()
            if text.startswith('Band ') and text.endswith(':'):
                band = _BAND_INDEX.get(text[5:-1].strip())
                if band:
                    survey[RX].setdefault(band, set())
                    survey[TX].setdefault(band, set())
                continue
            if band is None or 'MHz [' not in text:
                continue
            low = text.lower()
            if _UNUSABLE in low:
                continue
            try:
                channel = int(text.split('[', 1)[1].split(']', 1)[0])
            except (IndexError, ValueError):
                continue
            survey[RX][band].add(channel)
            if not any(flag in low for flag in _NO_TRANSMIT):
                survey[TX][band].add(channel)
    elif cached:
        return cached[0]
    told_us_something = bool(survey[RX])
    _phy_cache[phy] = (survey, time.monotonic(),
                       _PHY_TTL if told_us_something else _PHY_RETRY)
    return survey


def usable_channels(phy, band=None, mode=RX):
    if not phy:
        return set()
    survey = _phy_survey(phy).get(mode, {})
    if band is not None:
        return set(survey.get(band) or ())
    channels = set()
    for found in survey.values():
        channels |= found
    return channels


def transmit_channels(phy, band=None):
    return usable_channels(phy, band, mode=TX)


def bands_for(iface, default='2,5'):
    cached = _bands_cache.get(iface)
    if cached and (time.monotonic() - cached[1]) < _PHY_TTL:
        return cached[0]
    phy = phy_of(iface)
    if not phy:
        return default
    survey = _phy_survey(phy).get(RX, {})
    found = [band for band in ('2', '5', '6') if survey.get(band)]
    value = ','.join(found)
    if not value:
        return default
    if not cached or cached[0] != value:
        logging.info('%s (%s) can receive on band %s', iface, phy, value)
    _bands_cache[iface] = (value, time.monotonic())
    return value


def current_mac(iface):
    try:
        with open('/sys/class/net/%s/address' % iface) as f:
            mac = f.read().strip()
        return mac if len(mac) == 17 else None
    except OSError:
        return None


def permanent_mac(iface):
    rc, out, _ = system.run_cmd(['ethtool', '-P', iface], timeout=5)
    if rc == 0 and out:
        mac = out.strip().split()[-1]
        if len(mac) == 17 and mac != '00:00:00:00:00:00':
            return mac
    return None


def set_mac(iface, mac):
    if not mac or len(mac) != 17 or current_mac(iface) == mac:
        return True
    if not exists(iface):
        logging.debug('%s is gone, not setting its MAC', iface)
        return False
    system.run_cmd(['ip', 'link', 'set', iface, 'down'], timeout=10)
    rc, _, err = system.run_cmd(['ip', 'link', 'set', iface, 'address', mac], timeout=10)
    system.run_cmd(['ip', 'link', 'set', iface, 'up'], timeout=10)
    if rc != 0:
        logging.warning('could not restore %s MAC: %s', iface,
                        system.first_line(err))
        return False
    logging.info('restored %s MAC to %s', iface, mac)
    return True


def restore_mac(iface, remembered=None):
    if not exists(iface):
        logging.debug('%s is gone, nothing to restore', iface)
        return False
    wanted = remembered or permanent_mac(iface)
    if not wanted:
        return False
    if current_mac(iface) == wanted:
        return False
    return set_mac(iface, wanted)


class RadioPlan:
    def __init__(self, capture, pmkid, recons, mk7ac, warning):
        if recons is None:
            recons = []
        elif isinstance(recons, str):
            recons = [recons]
        self.capture = capture
        self.pmkid = pmkid
        self.recons = [i for i in recons if i]
        self.mk7ac = mk7ac
        self.warning = warning

    @property
    def recon(self):
        return self.recons[0] if self.recons else None

    @property
    def dedicated_pmkid(self):
        return self.pmkid is not None

    @property
    def keep_enabled(self):
        return [i for i in ([self.capture] + self.recons) if i]

    def describe(self):
        parts = ['capture %s' % self.capture]
        parts.append('PMKID %s' % self.pmkid if self.pmkid else 'PMKID sweep')
        if self.recons:
            parts.append('recon %s' % ', '.join(self.recons))
        return ' - '.join(parts)

    def __repr__(self):
        return '<RadioPlan capture=%s pmkid=%s recon=%s>' % (
            self.capture, self.pmkid, ','.join(self.recons) or None)


def resolve(choice, refresh=False, promote=None, avoid=()):
    choice = (choice or AUTO).strip().lower()
    if promote is None:
        promote = refresh
    status = mk7ac_status(refresh=refresh,
                          promote=promote and choice != BUILTIN_ONLY)
    ready = status == READY
    warning = None

    if choice == MK7AC_FIRST:
        if ready:
            capture = MK7AC
            pmkid = BUILTIN if is_usable(BUILTIN, refresh) else None
        else:
            capture = BUILTIN
            pmkid = None
            warning = ('MK7AC not in monitor mode - using built-in'
                       if status == NOT_MONITOR else
                       'MK7AC not found - using built-in')
    elif choice == BUILTIN_ONLY:
        capture = BUILTIN
        pmkid = None
    else:
        capture = BUILTIN
        pmkid = MK7AC if ready and is_usable(MK7AC, refresh) else None

    order = ((MK7AC, BUILTIN, SECONDARY) if choice == MK7AC_FIRST
             else (BUILTIN, MK7AC, SECONDARY))
    held = phy_pinned_by(capture, refresh) if exists(capture) else ()
    if not exists(capture) or held:
        spare = _first_monitor(order, refresh)
        if spare and spare != capture:
            warning = ('%s cannot tune while %s holds its phy - capturing on %s'
                       % (capture, held[0], spare) if held else
                       '%s missing - capturing on %s' % (capture, spare))
            capture = spare
            if pmkid == capture:
                pmkid = None

    recons = []
    for candidate in (SECONDARY, BUILTIN, MK7AC):
        if candidate in (capture, pmkid) or candidate in recons:
            continue
        if not exists(candidate) or not is_monitor(candidate, refresh=refresh):
            continue
        if phy_of(candidate) in avoid:
            continue
        if promote and phy_pinned_by(candidate, refresh=refresh):
            free_phy(candidate)
        if not phy_pinned_by(candidate, refresh=refresh):
            recons.append(candidate)

    if not exists(capture):
        blocked = [i for i in order
                   if exists(i) and is_monitor(i, refresh=refresh)
                   and phy_pinned_by(i, refresh=refresh)]
        if blocked:
            warning = warning or ('%s is the only monitor radio and %s already '
                                  'holds its phy' % (blocked[0],
                                                     phy_pinned_by(blocked[0])[0]))
        else:
            warning = warning or ('%s missing' % capture)
    return RadioPlan(capture, pmkid, recons, ready, warning)

