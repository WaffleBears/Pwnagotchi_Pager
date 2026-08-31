import glob
import json
import logging
import os
import threading
import time
from datetime import datetime

from . import config, pineap, system
from .scope import group_key

PMKID = '01'
MIN_CONVERT_AGE = 60
CONVERT_TIMEOUT = 120
MIN_CONVERT_SLICE = 30
FORCE_RESCAN = 120.0
CACHE_EVERY = 40
FOREIGN_PREFIX = 'foreign_'
EAPOL = '02'
CACHE_VERSION = 2
M1M2 = 0
PAIR_MASK = 0x07
CACHE_SAVE_EVERY = 60.0
MAX_TARGET_LOCKS = 256
EMPTY_CAPTURE_LIFE = 6 * 3600
MAX_ESSID_HEX = 64
MAX_CONVERT_TRIES = 3
MAX_EAPOL_HEX = 512
_HEX_DIGITS = frozenset('0123456789abcdefABCDEF')

_convert_lock = threading.Lock()
_registry = threading.Lock()
_target_locks = {}
_live = set()
_tmp_seq = [0]


def _target_lock(target):
    key = os.path.abspath(target)
    with _registry:
        lock = _target_locks.get(key)
        if lock is None:
            if len(_target_locks) >= MAX_TARGET_LOCKS:
                for stale in [k for k, l in _target_locks.items()
                              if not l.locked()][:MAX_TARGET_LOCKS // 2]:
                    del _target_locks[stale]
            lock = threading.Lock()
            _target_locks[key] = lock
        return lock


def _next_tmp(target):
    with _registry:
        _tmp_seq[0] += 1
        seq = _tmp_seq[0]
    return '%s.%d.%d.part' % (target, os.getpid(), seq)


def mark_live(path):
    if path:
        with _registry:
            _live.add(os.path.abspath(path))


def unmark_live(path):
    if path:
        with _registry:
            _live.discard(os.path.abspath(path))


def is_live(path):
    with _registry:
        return os.path.abspath(path) in _live


def _is_hex(s):
    return bool(s) and not (set(s) - _HEX_DIGITS)


def _is_hex_bytes(s, most, least=2):
    return least <= len(s) <= most and len(s) % 2 == 0 and _is_hex(s)


def _is_hash(s, length):
    return len(s) == length and _is_hex(s) and s.strip('0') != ''


def _message_pair(s):
    if len(s) != 2 or not _is_hex(s):
        return None
    return int(s, 16)


def _fmt_mac(raw):
    return ':'.join(raw[i:i + 2] for i in range(0, 12, 2))


def parse_line(line):
    line = line.strip()
    if not line.startswith('WPA*'):
        return None
    parts = line.split('*')
    if len(parts) < 6:
        return None
    kind, ap, sta = parts[1], parts[3].lower(), parts[4].lower()
    if kind not in (PMKID, EAPOL):
        return None
    if len(ap) != 12 or len(sta) != 12 or not _is_hex(ap) or not _is_hex(sta):
        return None
    named = _is_hex_bytes(parts[5], MAX_ESSID_HEX)
    if parts[5] and not named:
        return None
    try:
        essid = bytes.fromhex(parts[5]).decode('utf-8', errors='ignore')
    except ValueError:
        essid = ''
    strong = False
    if kind == PMKID:
        crackable = named and _is_hash(parts[2], 32)
        strong = crackable
    elif kind == EAPOL:
        pair = _message_pair(parts[8]) if len(parts) >= 9 else None
        crackable = (pair is not None and _is_hash(parts[2], 32)
                     and _is_hash(parts[6], 64)
                     and _is_hex_bytes(parts[7], MAX_EAPOL_HEX))
        strong = crackable and named and (pair & PAIR_MASK) != M1M2
    else:
        crackable = False
    return {
        'type': kind,
        'ap': _fmt_mac(ap),
        'sta': _fmt_mac(sta),
        'essid': essid,
        'crackable': crackable,
        'strong': strong,
        'key': '%s*%s*%s' % (kind, ap, sta),
    }


def _keep(store, rec):
    old = store.get(rec['key'])
    if old is None or rec.get('strong') or not old.get('strong'):
        store[rec['key']] = rec


def _parse_file(path):
    seen = {}
    rejected = 0
    unusable = 0
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = parse_line(line)
                if rec is None:
                    rejected += 1
                    continue
                if not rec['crackable']:
                    unusable += 1
                    continue
                rec['file'] = path
                _keep(seen, rec)
    except Exception as e:
        logging.warning('could not read %s: %s', os.path.basename(path), e)
        return None
    if rejected or unusable:
        logging.warning('%s: %d line(s) in an unknown format, %d not crackable',
                        os.path.basename(path), rejected, unusable)
    return list(seen.values())


class CaptureIndex:
    def __init__(self, path, cache_file=None, extra=()):
        self.path = path
        self.extra = [d for d in (extra or ()) if d and d != path]
        self.cache_file = cache_file
        self._lock = threading.Lock()
        self._scan = threading.Lock()
        self._cache = {}
        self._dirty = False
        self._built = False
        self.records = {}
        self.bssids = set()
        self.essids = set()
        self.groups = set()
        self.partials = set()
        self._dir_sig = None
        self._scanned_at = 0.0
        self._saved_at = 0.0
        self._cancel = threading.Event()
        self._load_cache()

    def _load_cache(self):
        if not self.cache_file or not os.path.exists(self.cache_file):
            return
        try:
            with open(self.cache_file) as fh:
                stored = json.load(fh)
        except Exception as e:
            logging.debug('capture cache unreadable: %s', e)
            return
        if not isinstance(stored, dict):
            return
        if stored.get('version') != CACHE_VERSION:
            logging.debug('capture cache is an old format, rebuilding')
            return
        files = stored.get('files')
        if not isinstance(files, dict):
            logging.debug('capture cache is malformed, rebuilding')
            return
        loaded = {}
        for name, entry in files.items():
            try:
                mtime, size, recs = entry
            except (TypeError, ValueError):
                continue
            if isinstance(recs, list) and all(isinstance(r, dict) for r in recs):
                loaded[name] = ((mtime, size), recs)
        self._cache = loaded

    def _save_cache(self, force=False):
        if not self.cache_file:
            return
        now = time.monotonic()
        with self._lock:
            if not self._dirty:
                return
            if not force and now - self._saved_at < CACHE_SAVE_EVERY:
                return
            self._saved_at = now
            payload = {'version': CACHE_VERSION,
                       'files': {name: [sig[0], sig[1], recs]
                                 for name, (sig, recs) in self._cache.items()}}
            self._dirty = False
        try:
            parent = os.path.dirname(self.cache_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.cache_file + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.cache_file)
        except Exception as e:
            logging.debug('could not save capture cache: %s', e)
            with self._lock:
                self._dirty = True

    def flush(self):
        self._save_cache(force=True)

    def cancel(self):
        self._cancel.set()

    def _store(self, parsed, files):
        with self._lock:
            for name, recs in parsed.items():
                if name in files:
                    self._cache[name] = (files[name], recs)
                    self._dirty = True

    def _folders(self):
        return [self.path] + self.extra

    def _dir_signature(self):
        sig = []
        for folder in self._folders():
            try:
                st = os.stat(folder)
                held = len(os.listdir(folder))
            except OSError:
                sig.append((folder, None))
                continue
            sig.append((folder, st.st_mtime, st.st_size, held))
        return tuple(sig)

    def refresh(self, force=False):
        if not self._scan.acquire(False):
            return []
        try:
            now = time.monotonic()
            sig = self._dir_signature()
            fresh = now - self._scanned_at >= FORCE_RESCAN
            if not force and not fresh and self._built and sig == self._dir_sig:
                return []
            self._dir_sig = sig
            self._scanned_at = now
            return self._refresh()
        finally:
            self._scan.release()

    def _refresh(self):
        files = {}
        for folder in self._folders():
            for f in glob.glob(os.path.join(folder, '*.22000')):
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                files[f] = (st.st_mtime, st.st_size)

        with self._lock:
            stale = [f for f, sig in files.items()
                     if f not in self._cache or self._cache[f][0] != sig]
            removed = [f for f in self._cache if f not in files]
            for f in removed:
                del self._cache[f]
                self._dirty = True
            if self._built and not stale and not removed:
                return []

        parsed = {}
        aborted = False
        for name in stale:
            if self._cancel.is_set():
                aborted = True
                break
            recs = _parse_file(name)
            if recs is None:
                continue
            parsed[name] = recs
            if len(parsed) >= CACHE_EVERY:
                self._store(parsed, files)
                parsed = {}
                self._save_cache()

        with self._lock:
            for f, recs in parsed.items():
                if f in files:
                    self._cache[f] = (files[f], recs)
                    self._dirty = True
            records = {}
            for f in files:
                entry = self._cache.get(f)
                if entry:
                    for rec in entry[1]:
                        _keep(records, rec)
            was_strong = {k for k, r in self.records.items()
                          if r.get('strong')}
            new_keys = set(records) - set(self.records)
            upgraded = {k for k, r in records.items()
                        if r.get('strong') and k not in was_strong
                        and k not in new_keys}
            new_keys |= upgraded
            if upgraded:
                logging.info('%d capture(s) upgraded to a full handshake',
                             len(upgraded))
            self.records = records
            self.bssids = {r['ap'].lower() for r in records.values()
                           if r.get('strong')}
            self.partials = {r['ap'].lower() for r in records.values()
                             if r['crackable'] and not r.get('strong')}
            self.partials -= self.bssids
            self.essids = {r['essid'] for r in records.values()
                           if r.get('strong') and r['essid']}
            self.groups = {k for k in (group_key(b) for b in self.bssids) if k}
            fresh = [records[k] for k in new_keys]
            self._built = not aborted
        self._save_cache()
        return fresh

    def holds(self, bssid, essid=None, cover_siblings=False):
        bssid = (bssid or '').lower()
        if bssid and bssid in self.bssids:
            return True
        if not cover_siblings:
            return False
        if self.holds_essid(essid):
            return True
        if essid:
            return False
        return group_key(bssid) in self.groups

    def holds_essid(self, essid):
        return bool(essid) and essid in self.essids

    def networks(self):
        out = set()
        for r in self.records.values():
            if r['crackable']:
                out.add(r['essid'] or r['ap'].lower())
        return out

    def network_count(self):
        return len(self.networks())

    def strong_networks(self):
        out = set()
        for r in self.records.values():
            if r.get('strong'):
                out.add(r['essid'] or r['ap'].lower())
        return out

    def weak_networks(self):
        return self.networks() - self.strong_networks()


def _stall_count(marker, signature):
    try:
        with open(marker) as fh:
            stored, count = fh.read().strip().rsplit(' ', 1)
    except (OSError, ValueError):
        return 0
    if stored != signature or not count.isdigit():
        return 0
    return int(count)


def _note_stall(marker, signature, count):
    try:
        with open(marker, 'w') as fh:
            fh.write('%s %d' % (signature, count))
    except OSError:
        pass


def _drop(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _marked_empty(marker, signature):
    try:
        with open(marker) as fh:
            return fh.read().strip() == signature
    except OSError:
        return False


def _fresher_than(target, source):
    try:
        return os.path.getmtime(target) >= os.path.getmtime(source)
    except OSError:
        return False


def run_hcxpcapngtool(source, target, timeout=CONVERT_TIMEOUT, csv=None):
    with _target_lock(target):
        if os.path.exists(target) and _fresher_than(target, source):
            return True
        tmp = _next_tmp(target)
        argv = ['hcxpcapngtool', '-o', tmp]
        if csv:
            argv.append('--csv=%s' % csv)
        argv.append(source)
        rc, _, err = system.run_cmd(argv, timeout=timeout)
        try:
            if rc != 0:
                if rc < 0:
                    logging.warning('converting %s did not finish (%s), will retry',
                                    os.path.basename(source),
                                    system.first_line(err) or 'timeout')
                if os.path.exists(tmp):
                    os.remove(tmp)
                return None
            if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                if os.path.exists(tmp):
                    os.remove(tmp)
                return False
            parsed = _parse_file(tmp)
            if parsed is None:
                logging.warning('could not read the converted %s, keeping the '
                                'source to retry', os.path.basename(source))
                os.remove(tmp)
                return None
            if not parsed:
                logging.info('%s produced no crackable hash, dropping it',
                             os.path.basename(source))
                os.remove(tmp)
                return False
            os.replace(tmp, target)
            return True
        except OSError as e:
            logging.debug('could not publish %s: %s', target, e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return None


def sweep_partials(path, older_than=3600):
    cutoff = time.time() - older_than
    for junk in glob.glob(os.path.join(path, '*.part')):
        try:
            if os.path.getmtime(junk) < cutoff:
                os.remove(junk)
        except OSError:
            continue
    for mark in glob.glob(os.path.join(path, '*.slow')):
        base = mark[:-len('.slow')]
        if not any(os.path.exists(base + s) for s in ('.pcapng', '.pcap')):
            _drop(mark)


def sweep_barren(path, older_than=EMPTY_CAPTURE_LIFE):
    cutoff = time.time() - older_than
    dropped = 0
    for marker in glob.glob(os.path.join(path, '*.nohash')):
        base = marker[:-len('.nohash')]
        raw = [base + suffix for suffix in ('.pcapng', '.pcap')]
        alive = [p for p in raw if os.path.exists(p)]
        if any(is_live(p) for p in alive):
            continue
        try:
            if os.path.getmtime(marker) >= cutoff:
                continue
            if os.path.exists(base + '.22000'):
                os.remove(marker)
                continue
            for p in alive:
                os.remove(p)
                dropped += 1
            os.remove(marker)
        except OSError:
            continue
    if dropped:
        logging.info('cleared %d raw capture(s) that held no handshake',
                     dropped)
    return dropped


def convert_pcaps(path, limit=250, min_age=MIN_CONVERT_AGE, budget=None,
                  extra=()):
    if not system.have('hcxpcapngtool'):
        return 0
    if not _convert_lock.acquire(False):
        return 0
    try:
        sweep_partials(path)
        sweep_barren(path)
        return _convert_pcaps(path, limit, min_age, budget, extra)
    finally:
        _convert_lock.release()


def _convert_pcaps(path, limit, min_age, budget, extra=()):
    converted = 0
    attempted = 0
    cutoff = time.time() - min_age
    deadline = None if not budget else time.monotonic() + budget
    sources = []
    for folder in [path] + [d for d in (extra or ()) if d and d != path]:
        found = sorted(glob.glob(os.path.join(folder, '*.pcap'))
                       + glob.glob(os.path.join(folder, '*.pcapng')))
        sources.extend((f, folder == path) for f in found)
    for p, own in sources:
        if attempted >= limit:
            logging.info('converted the first %d capture(s), the rest wait '
                         'for the next pass', limit)
            break
        if deadline is not None and time.monotonic() >= deadline:
            logging.info('conversion budget spent after %d file(s), '
                         'the rest wait for the next pass', attempted)
            break
        if is_live(p):
            continue
        try:
            if os.path.getmtime(p) > cutoff:
                continue
        except OSError:
            continue
        if own:
            base = p.rsplit('.', 1)[0]
        else:
            stem = os.path.basename(p).rsplit('.', 1)[0]
            base = os.path.join(path, FOREIGN_PREFIX + stem)
        target = base + '.22000'
        marker = base + '.nohash'
        if os.path.exists(target) and _fresher_than(target, p):
            continue
        try:
            signature = '%d %d' % (os.path.getmtime(p), os.path.getsize(p))
        except OSError:
            continue
        if _marked_empty(marker, signature):
            continue
        stalled = base + '.slow'
        tries = _stall_count(stalled, signature)
        if tries >= MAX_CONVERT_TRIES:
            continue
        attempted += 1
        share = CONVERT_TIMEOUT
        if deadline is not None:
            share = max(MIN_CONVERT_SLICE,
                        int(deadline - time.monotonic()))
        outcome = run_hcxpcapngtool(p, target, timeout=share)
        if outcome is True:
            converted += 1
            _drop(marker, stalled)
        elif outcome is False:
            try:
                with open(marker, 'w') as fh:
                    fh.write(signature)
            except OSError:
                pass
            _drop(stalled)
        else:
            _note_stall(stalled, signature, tries + 1)
            if tries + 1 >= MAX_CONVERT_TRIES:
                logging.warning('%s has failed to convert %d times, skipping '
                                'it so the rest of the backlog can run',
                                os.path.basename(p), tries + 1)
    return converted


MAX_SEEN = 4000


class APLog:
    def __init__(self, settings):
        self.settings = settings
        self._seen = {}
        self._file = None

    @property
    def enabled(self):
        return self.settings.get('log_aps_enabled')

    def reload(self):
        self._ensure_file()

    def start(self):
        self._ensure_file()

    def _ensure_file(self):
        if not self.enabled or self._file:
            return
        try:
            os.makedirs(config.AP_LOG_DIR, exist_ok=True)
        except Exception as e:
            logging.error('ap log dir: %s', e)
            return
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self._file = os.path.join(config.AP_LOG_DIR, 'aps_%s.jsonl' % stamp)
        logging.info('AP log: %s', self._file)

    def _remember(self, key):
        if len(self._seen) >= MAX_SEEN:
            for old in list(self._seen)[:MAX_SEEN // 4]:
                del self._seen[old]
        self._seen[key] = True

    def log(self, aps):
        if not self.enabled or not aps:
            return
        self._ensure_file()
        if not self._file:
            return
        stamp = datetime.now().isoformat()
        rows = []
        for ap in aps:
            mac = ap.get('mac', '')
            if not mac or mac in self._seen:
                continue
            self._remember(mac)
            rows.append({
                'timestamp': stamp,
                'mac': mac,
                'ssid': ap.get('hostname', ''),
                'hidden': ap.get('hidden', False),
                'channel': ap.get('channel', 0),
                'band': pineap.channel_band(ap.get('channel', 0), ap.get('band')),
                'rssi': ap.get('rssi', -100),
                'clients': len(ap.get('clients', [])),
            })
        if not rows:
            return
        try:
            with open(self._file, 'a') as f:
                for entry in rows:
                    f.write(json.dumps(entry) + '\n')
        except Exception as e:
            logging.error('ap log write: %s', e)

    def stop(self):
        if self._file:
            logging.info('finished log %s', self._file)
