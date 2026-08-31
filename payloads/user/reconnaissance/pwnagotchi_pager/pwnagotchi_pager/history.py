import json
import logging
import os
import threading
import time

from . import config, pineap as pineap_mod

APPROACH_TTL = 30 * 24 * 3600
MAX_APPROACH = 256
NO_INJECT_TTL = 30 * 24 * 3600
MAX_NO_INJECT = 256
LEDGER_TTL = 14 * 24 * 3600
LEDGER_FADE = 24 * 3600
MAX_LEDGER = 512
MAX_FATIGUE = 512
MAX_REFUSED = 512
FATIGUE_PENALTY = 12
LEDGER_PENALTY = 20
MAX_LEDGER_PENALTY = 3
INFINITIES = (float('inf'), float('-inf'))


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in INFINITIES:
        return None
    return number


def stored_pairs(path):
    present = os.path.exists(path)
    try:
        with open(path) as fh:
            stored = json.load(fh)
    except Exception as e:
        if present:
            logging.warning('could not read %s: %s', os.path.basename(path), e)
        return {}, [], not present
    if not isinstance(stored, dict) or not isinstance(stored.get('aps'), dict):
        return {}, [], not present
    out = []
    for mac, entry in stored['aps'].items():
        try:
            first, seen = finite(entry[0]), finite(entry[1])
        except (TypeError, IndexError, KeyError):
            continue
        if first is None or seen is None:
            continue
        out.append((str(mac).lower(), first, seen))
    return stored, out, True


def _write_pairs(path, aps, readable, what):
    try:
        if not aps:
            if readable and os.path.exists(path):
                os.remove(path)
            return
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump({'aps': {m: [c, s] for m, (c, s) in aps.items()}}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception as e:
        logging.debug('could not save the %s: %s', what, e)


class History:
    def __init__(self, settings, domain='', guarded=None):
        self.settings = settings
        self.domain = domain
        self._guarded = guarded or (lambda mac: False)
        self._lock = threading.RLock()
        self._fatigue = {}
        self._faded_at = {}
        self._attacked = {}
        self._pending = []
        self._ledger_readable = True
        self._approach_readable = True
        self._no_inject_readable = True
        self._no_inject_seen = {}
        self._ledger = self._load_ledger()
        self._approach = self._load_approach()
        self._refused = self._load_no_inject()

    def _tune(self, key):
        return self.settings.tune(key)

    def _load_ledger(self):
        now = time.time()
        out = {}
        stored, pairs, ok = stored_pairs(config.LEDGER_FILE)
        self._ledger_readable = ok
        for mac, misses, seen in pairs:
            misses = int(misses)
            periods = int(max(0.0, now - seen) / LEDGER_FADE)
            faded = misses - periods
            if faded > 0 and now - seen < LEDGER_TTL:
                out[mac] = (faded, seen + periods * LEDGER_FADE)
        if out:
            logging.info('%d AP(s) resisted before, starting them lower', len(out))
        return out

    def _load_approach(self):
        now = time.time()
        out = {}
        stored, pairs, ok = stored_pairs(config.APPROACH_FILE)
        self._approach_readable = ok
        for mac, misses, seen in pairs:
            if now - seen > APPROACH_TTL:
                continue
            misses = int(misses)
            if misses > 0:
                out[mac] = (misses, seen)
        if out:
            logging.info('%d AP(s) never gave up a handshake to deauth before',
                         len(out))
        return out

    def _load_no_inject(self):
        self._no_inject_seen = {}
        here = self.domain
        if not here:
            return {}
        stored, pairs, ok = stored_pairs(config.NO_INJECT_FILE)
        self._no_inject_readable = ok
        if not stored:
            return {}
        was = stored.get('domain')
        if was != here:
            logging.info('regulatory domain is %s now, not %s - '
                         'forgetting the old no-inject list', here, was)
            return {}
        probe = max(1, self._tune('max_deauth_refusals') - 1)
        now = time.time()
        out = {}
        for mac, channel, seen in pairs:
            channel = int(channel)
            if not pineap_mod.valid_channel(channel):
                continue
            if now - seen > NO_INJECT_TTL:
                continue
            out[mac] = (probe, channel)
            self._no_inject_seen[mac] = seen
        if out:
            logging.info('%d AP(s) in %s refused injection before, '
                         're-checking each once', len(out), here)
        return out

    def tiredness(self, mac):
        with self._lock:
            return self._fatigue.get(mac, 0)

    def worn_out(self, mac):
        return self.tiredness(mac) >= self._tune('stubborn_epochs')

    def tire(self, mac, level=None):
        with self._lock:
            self._fatigue[mac] = self._fatigue.get(mac, 0) + 1
            if level is not None:
                self._faded_at[mac] = level

    def rest(self, mac):
        with self._lock:
            self._fatigue.pop(mac, None)
            self._faded_at.pop(mac, None)

    def misses(self, mac):
        with self._lock:
            entry = self._ledger.get(mac)
        return entry[0] if entry else 0

    def ignored_deauths(self, mac):
        with self._lock:
            entry = self._approach.get(mac)
        return entry[0] if entry else 0

    def refusals(self, mac):
        with self._lock:
            entry = self._refused.get(mac)
        return entry[0] if entry else 0

    def faded_at(self, mac):
        with self._lock:
            return self._faded_at.get(mac)

    def counts(self):
        with self._lock:
            return {'fatigue': len(self._fatigue), 'faded': len(self._faded_at),
                    'refused': len(self._refused), 'ledger': len(self._ledger),
                    'approach': len(self._approach),
                    'pending': len(self._pending),
                    'attacked': len(self._attacked),
                    'stray_faded': len(set(self._faded_at) - set(self._fatigue))}

    def penalty(self, mac):
        with self._lock:
            tired = self._fatigue.get(mac, 0)
            entry = self._ledger.get(mac)
        misses = min(MAX_LEDGER_PENALTY, entry[0]) if entry else 0
        return FATIGUE_PENALTY * tired + LEDGER_PENALTY * misses

    def note_refusal(self, ap):
        mac = ap.get('mac', '').lower()
        channel = ap.get('channel', 0)
        with self._lock:
            count, seen_on = self._refused.get(mac, (0, channel))
            if seen_on != channel:
                count = 0
            count += 1
            self._refused[mac] = (count, channel)
        return count

    def forget_refusal(self, mac):
        with self._lock:
            self._refused.pop(mac, None)
            self._no_inject_seen.pop(mac, None)

    def refuses_injection(self, ap):
        with self._lock:
            count, seen_on = self._refused.get(ap.get('mac', '').lower(), (0, 0))
        return (seen_on == ap.get('channel', 0)
                and count >= self._tune('max_deauth_refusals'))

    def give_up_after(self, mac):
        limit = self._tune('deauth_dead_after')
        if self._guarded(mac):
            return max(2, limit // 2)
        return limit

    def is_deauth_dead(self, mac):
        with self._lock:
            entry = self._approach.get(mac)
        return bool(entry) and entry[0] >= self.give_up_after(mac)

    def note_approach(self, mac, hit):
        with self._lock:
            if hit:
                self._approach.pop(mac, None)
                return False
            misses, _ = self._approach.get(mac, (0, 0.0))
            self._approach[mac] = (misses + 1, time.time())
        return misses + 1 == self.give_up_after(mac)

    def note_outcome(self, mac, captured):
        with self._lock:
            if captured:
                self._ledger.pop(mac, None)
                return
            misses, _ = self._ledger.get(mac, (0, 0.0))
            self._ledger[mac] = (misses + 1, time.time())

    def record_attack(self, mac, held, aimed):
        with self._lock:
            was_held, was_aimed = self._attacked.get(mac, (held, False))
            self._attacked[mac] = (was_held, was_aimed or aimed)

    def take_attacked(self):
        with self._lock:
            taken = set(self._attacked)
            self._attacked = {}
        return taken

    def recently_attacked(self, mac, delay):
        cutoff = time.monotonic() - delay
        with self._lock:
            if mac in self._attacked:
                return True
            pending = list(self._pending)
        return any(m == mac and when > cutoff for m, when, _, _ in pending)

    def settle(self, captured, delay):
        due = []
        waiting = []
        cutoff = time.monotonic() - delay
        with self._lock:
            outstanding = list(self._pending)
            attacked = dict(self._attacked)
        for mac, when, had, aimed in outstanding:
            if (mac in captured and not had) or when <= cutoff:
                due.append((mac, had, aimed))
            else:
                waiting.append((mac, when, had, aimed))
        now = time.monotonic()
        pending = {mac for mac, _, _, _ in waiting}
        for mac, (had, aimed) in attacked.items():
            if mac in captured and not had:
                due.append((mac, had, aimed))
            elif mac not in pending:
                waiting.append((mac, now, had, aimed))
        return due, waiting

    def set_pending(self, waiting):
        with self._lock:
            self._pending = waiting

    def forgive(self, spare, busy, heard, forgiving):
        gain = self._tune('signal_forgive_db')
        with self._lock:
            for mac in list(self._fatigue):
                if mac in spare:
                    continue
                was = self._faded_at.get(mac)
                louder = (was is not None and mac in heard
                          and heard[mac] - was >= gain)
                if louder:
                    logging.debug('%s is %d dB louder than when it last '
                                  'resisted, giving it another go',
                                  mac, heard[mac] - was)
                if not forgiving and not louder and mac not in busy:
                    continue
                left = self._fatigue.get(mac, 0) - 1
                if left > 0:
                    self._fatigue[mac] = left
                    if louder:
                        self._faded_at[mac] = heard[mac]
                else:
                    self._fatigue.pop(mac, None)
                    self._faded_at.pop(mac, None)

    def trim(self):
        now = time.time()
        with self._lock:
            if len(self._fatigue) > MAX_FATIGUE:
                keep = sorted(self._fatigue.items(),
                              key=lambda kv: -kv[1])[:MAX_FATIGUE]
                self._fatigue = dict(keep)
            for mac in list(self._faded_at):
                if mac not in self._fatigue:
                    self._faded_at.pop(mac, None)
            if len(self._refused) > MAX_REFUSED:
                limit = self._tune('max_deauth_refusals')

                def worth_keeping(mac):
                    count = self._refused.get(mac, (0, 0))[0]
                    seen = self._no_inject_seen.get(mac)
                    if seen is not None:
                        return (2, seen)
                    if count >= limit:
                        return (1, now)
                    return (0, now)

                order = sorted(self._refused, key=worth_keeping, reverse=True)
                for mac in order[MAX_REFUSED:]:
                    self._refused.pop(mac, None)
                    self._no_inject_seen.pop(mac, None)

    def rebase_domain(self, here):
        self.domain = here
        with self._lock:
            self._refused = {}
            self._no_inject_seen = {}

    def save_approach(self):
        with self._lock:
            aps = dict(self._approach)
        if len(aps) > MAX_APPROACH:
            aps = dict(sorted(aps.items(), key=lambda kv: -kv[1][1])[:MAX_APPROACH])
        _write_pairs(config.APPROACH_FILE, aps, self._approach_readable,
                     'approach ledger')

    def save_ledger(self):
        with self._lock:
            aps = dict(self._ledger)
        if len(aps) > MAX_LEDGER:
            aps = dict(sorted(aps.items(), key=lambda kv: -kv[1][1])[:MAX_LEDGER])
        _write_pairs(config.LEDGER_FILE, aps, True, 'hunt ledger')

    def save_no_inject(self):
        here = self.domain
        limit = self._tune('max_deauth_refusals')
        now = time.time()
        aps = {}
        with self._lock:
            refused = dict(self._refused)
            remembered = dict(self._no_inject_seen)
        for mac, (count, channel) in refused.items():
            if not channel:
                continue
            if count >= limit:
                aps[mac] = [channel, now]
            elif mac in remembered:
                aps[mac] = [channel, remembered[mac]]
        aps = {m: v for m, v in aps.items() if now - v[1] <= NO_INJECT_TTL}
        if len(aps) > MAX_NO_INJECT:
            newest = sorted(aps.items(), key=lambda kv: -kv[1][1])[:MAX_NO_INJECT]
            aps = dict(newest)
        if not here:
            logging.debug('regulatory domain unknown, leaving the '
                          'no-inject list untouched')
            return
        if not aps:
            try:
                if self._no_inject_readable and os.path.exists(
                        config.NO_INJECT_FILE):
                    os.remove(config.NO_INJECT_FILE)
            except OSError as e:
                logging.debug('could not clear the no-inject list: %s', e)
            return
        try:
            tmp = config.NO_INJECT_FILE + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump({'domain': here, 'aps': aps}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, config.NO_INJECT_FILE)
        except Exception as e:
            logging.debug('could not save the no-inject list: %s', e)

    def save(self):
        self.save_approach()
        self.save_ledger()
        self.save_no_inject()
