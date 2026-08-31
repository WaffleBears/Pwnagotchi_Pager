import json
import logging
import os
import re
import subprocess
import threading
import time

from . import captures, conditions, interfaces, pineap, system

RESTART_COOLDOWN = 90
CONVERT_INTERVAL = 60
MAX_LAUNCH_FAILURES = 3
ROTATE_CONVERT = 30
SHUTDOWN_CONVERT = 20
ROTATE_AFTER = 900
MAX_BPF_TARGETS = 512
BPF_COMPILE_TIMEOUT = 20
STAY_TIME = 2
MAX_STAY_TIME = 8
SWEEP_CYCLE = 24
MAC_STATE = '.capture_macs.json'
MAX_NAME_TRIES = 64
FALLBACK_RETRY = 600
PASSIVE_FLAGS = ('--disable_deauthentication', '--disable_association',
                 '--disable_reassociation', '--disable_proberequest',
                 '--disable_beacon')
FOCUS_TARGETS = 8
FOCUS_ATTEMPT_AP = 16
FOCUS_ATTEMPT_CLIENT = 24
ATTEMPT_AP = 8
ATTEMPT_CLIENT = 12
MAX_ATTEMPT = 64
ERROR_MAX = 512
DEAF_TICKS = 2

OFF = 'off'
UNAVAILABLE = 'unavailable'
DEDICATED = 'dedicated'
SWEEP = 'sweep'
HELD = 'held'


MAX_SECURITY = 512
PROTECTED_AKM = ('SAE', 'OWE', 'FT-SAE', '802.1X-SUITE-B')


def tool_available():
    return system.have('hcxdumptool')


def read_security(path):
    found = {}
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
            for line in fh:
                parts = line.rstrip('\n').split('\t')
                if len(parts) < 7:
                    continue
                mac = parts[2].strip().lower()
                if len(mac) != 17 or mac.count(':') != 5:
                    continue
                start = None
                for i in range(3, len(parts) - 2):
                    if parts[i].startswith('[') and parts[i].endswith(']'):
                        start = i
                        break
                if start is None:
                    continue
                akm = frozenset(t for t in
                                parts[start + 2].strip('[]').split('][') if t)
                found[mac] = {'enc': parts[start].strip('[]'),
                              'cipher': parts[start + 1].strip('[]'),
                              'akm': akm}
    except OSError:
        return {}
    return found


def needs_protection(akm):
    return any(a in PROTECTED_AKM or a.startswith('SAE') or a.startswith('OWE')
               for a in akm or ())


def only_protection(akm):
    return bool(akm) and all(a in PROTECTED_AKM or a.startswith('SAE')
                             or a.startswith('OWE') for a in akm)


def mac_state_file(scratch_dir):
    return os.path.join(scratch_dir, MAC_STATE)


def _read_mac_state(scratch_dir):
    try:
        with open(mac_state_file(scratch_dir)) as fh:
            stored = json.load(fh)
    except Exception:
        return {}
    return stored if isinstance(stored, dict) else {}


def _write_mac_state(scratch_dir, state):
    path = mac_state_file(scratch_dir)
    try:
        if not state:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(scratch_dir, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception as e:
        logging.debug('could not record capture MACs: %s', e)


def remember_mac(scratch_dir, iface, mac):
    if not iface or not mac:
        return
    state = _read_mac_state(scratch_dir)
    if state.get(iface) == mac:
        return
    state[iface] = mac
    _write_mac_state(scratch_dir, state)


def forget_mac(scratch_dir, iface):
    if not iface:
        return
    state = _read_mac_state(scratch_dir)
    if state.pop(iface, None) is None:
        return
    _write_mac_state(scratch_dir, state)


def recover_previous(scratch_dir):
    stale = system.run_cmd(['killall', 'hcxdumptool'], timeout=10)[0] == 0
    if stale:
        logging.warning('an hcxdumptool from an earlier run was still holding '
                        'a radio, stopped it')
        time.sleep(1)
    state = _read_mac_state(scratch_dir)
    for iface, mac in list(state.items()):
        try:
            if interfaces.restore_mac(iface, mac):
                logging.warning('restored the %s MAC left randomised by an '
                                'unclean shutdown', iface)
        except Exception as e:
            logging.debug('MAC recovery on %s: %s', iface, e)
    if state:
        _write_mac_state(scratch_dir, {})
    return stale


BAND_LETTER = {
    pineap.BAND_2G: 'a',
    pineap.BAND_5G: 'b',
    pineap.BAND_6G: 'c',
}


def channel_spec(channels):
    tokens = set()
    if isinstance(channels, (str, bytes)) or not hasattr(channels, '__iter__'):
        channels = ()
    for entry in channels or ():
        if isinstance(entry, (tuple, list)):
            channel, band = (list(entry) + [None, None])[:2]
        else:
            channel, band = entry, None
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            continue
        if not channel:
            continue
        letter = BAND_LETTER.get(band or pineap.band_for_channel(channel))
        if letter is None:
            logging.warning('cannot tune channel %s: unknown band %r',
                            channel, band)
            continue
        tokens.add((channel, letter))
    return ','.join('%d%s' % t for t in sorted(tokens))


def attempts(base, patience):
    try:
        scaled = int(round(base * float(patience)))
    except (TypeError, ValueError):
        scaled = base
    return max(1, min(MAX_ATTEMPT, scaled))


def stay_time(spec):
    hops = len(spec.split(',')) if spec else 0
    if hops <= 1:
        return STAY_TIME if not hops else MAX_STAY_TIME
    return max(STAY_TIME, min(MAX_STAY_TIME, SWEEP_CYCLE // hops))


def bpf_mac(value):
    raw = re.sub(r'[^0-9a-fA-F]', '', str(value or '')).lower()
    if len(raw) != 12:
        return None
    return ':'.join(raw[i:i + 2] for i in range(0, 12, 2))


def _compile_expr(macs):
    expr = ' or '.join('wlan addr3 %s' % m for m in macs)
    rc, out, err = system.run_cmd(['hcxdumptool', '--bpfc=%s' % expr],
                                  timeout=BPF_COMPILE_TIMEOUT)
    if rc != 0 or not out.strip():
        return '', system.first_line(err) or system.first_line(out)
    return out.strip(), ''


_compiled = {}


def compile_bpf(bssids, path):
    wanted = []
    for mac in (bpf_mac(b) for b in bssids or ()):
        if mac and mac not in wanted:
            wanted.append(mac)
    if not wanted:
        return False
    if _compiled.get(path) == tuple(wanted) and os.path.exists(path):
        return True
    limit = min(len(wanted), MAX_BPF_TARGETS)
    program, reason = '', ''
    while limit >= 1:
        program, reason = _compile_expr(wanted[:limit])
        if program:
            break
        if limit == 1:
            break
        limit = max(1, limit // 2)
    if not program:
        logging.warning('BPF compile failed (%s)', reason or 'no reason given')
        return False
    if limit < len(wanted):
        logging.warning('BPF filter holds %d of %d targets, the rest are skipped',
                        limit, len(wanted))
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            fh.write(program + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _compiled[path] = tuple(wanted)
        return True
    except Exception as e:
        logging.warning('could not write BPF: %s', e)
        _compiled.pop(path, None)
        return False


class Capture:
    def __init__(self, out_dir, prefix, scratch_dir=None):
        self.out_dir = out_dir
        self.prefix = prefix
        self.scratch_dir = scratch_dir or out_dir
        self.proc = None
        self.pcap = None
        self.hashes = None
        self.csv = None
        self.iface = None
        self.saved_mac = None
        self._seq = 0
        self._last_size = -1
        self._converting = threading.Lock()

    def _next_base(self):
        stamp = int(time.time())
        for _ in range(MAX_NAME_TRIES):
            self._seq += 1
            base = os.path.join(self.out_dir, '%s_%d_%d'
                                % (self.prefix, stamp, self._seq))
            if not os.path.exists(base + '.pcapng') and not os.path.exists(base + '.22000'):
                return base
        return os.path.join(self.out_dir, '%s_%d_%d_%d'
                            % (self.prefix, stamp, os.getpid(), self._seq))

    def build_cmd(self, iface, targets, channels, bpf_path, passive=False,
                  with_channels=True, patience=1.0, essids=()):
        if targets is not None:
            if not targets:
                return None
            if not compile_bpf(targets, bpf_path):
                logging.warning('refusing to capture without the target filter')
                return None
        self.iface = iface
        with self._converting:
            captures.unmark_live(self.pcap)
            base = self._next_base()
            self.pcap = base + '.pcapng'
            self.hashes = base + '.22000'
            self.csv = os.path.join(self.scratch_dir,
                                    '%s_seen_aps.csv' % self.prefix)
            self._last_size = -1
        try:
            if os.path.exists(self.csv):
                os.remove(self.csv)
        except OSError:
            pass
        spec = channel_spec(channels) if (channels and with_channels) else ''
        cmd = ['hcxdumptool', '-i', iface, '-w', self.pcap,
               '-t', str(stay_time(spec))]
        if spec:
            cmd += ['-c', spec]
        else:
            cmd.append('-F')
        if targets is not None:
            cmd.append('--bpf=%s' % bpf_path)
        cmd.append('--errormax=%d' % ERROR_MAX)
        if passive:
            cmd.extend(PASSIVE_FLAGS)
        else:
            listed = self._write_essids(essids)
            if listed:
                cmd.append('--essidlist=%s' % listed)
            focused = targets is not None and 0 < len(targets) <= FOCUS_TARGETS
            ap_max = FOCUS_ATTEMPT_AP if focused else ATTEMPT_AP
            client_max = FOCUS_ATTEMPT_CLIENT if focused else ATTEMPT_CLIENT
            cmd.append('--attemptapmax=%d' % attempts(ap_max, patience))
            cmd.append('--attemptclientmax=%d'
                       % attempts(client_max, patience))
        return cmd

    def _write_essids(self, essids):
        path = os.path.join(self.scratch_dir, '%s_essids.txt' % self.prefix)
        names = [n for n in (essids or ()) if n]
        if not names:
            try:
                os.remove(path)
            except OSError:
                pass
            return ''
        try:
            with open(path, 'w') as fh:
                fh.write('\n'.join(names) + '\n')
        except OSError as e:
            logging.debug('cannot write the ESSID list: %s', e)
            return ''
        return path

    def spawn(self, cmd, settle=2.0):
        if self.alive():
            logging.warning('refusing to start a second hcxdumptool on %s',
                            self.iface)
            return False
        if self.iface:
            interfaces.ensure_up(self.iface)
            self.saved_mac = interfaces.permanent_mac(self.iface)
            if not self.saved_mac:
                self.saved_mac = _read_mac_state(self.scratch_dir).get(self.iface)
            if not self.saved_mac:
                self.saved_mac = interfaces.current_mac(self.iface)
                if not self.saved_mac:
                    logging.warning('cannot read the MAC of %s - it will not '
                                    'be restored after capture', self.iface)
            remember_mac(self.scratch_dir, self.iface, self.saved_mac)
        captures.mark_live(self.pcap)
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except Exception as e:
            logging.warning('hcxdumptool would not start: %s', e)
            self.proc = None
            captures.unmark_live(self.pcap)
            self.restore_iface()
            return False
        time.sleep(settle)
        if self.proc.poll() is not None:
            logging.warning('hcxdumptool exited immediately')
            self.proc = None
            captures.unmark_live(self.pcap)
            self.restore_iface()
            return False
        return True

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def bytes_written(self):
        try:
            return os.path.getsize(self.pcap) if self.pcap else 0
        except OSError:
            return 0

    def halt(self):
        proc = self.proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            except Exception:
                pass
            if proc.poll() is None:
                logging.warning('hcxdumptool on %s would not die, leaving it',
                                self.iface)
                return False
            self.proc = None
        else:
            self.proc = None
        captures.unmark_live(self.pcap)
        self.restore_iface()
        return True

    def restore_iface(self):
        if not self.iface:
            return
        try:
            interfaces.restore_mac(self.iface, self.saved_mac)
        except Exception as e:
            logging.debug('MAC restore on %s: %s', self.iface, e)
        forget_mac(self.scratch_dir, self.iface)

    def convert(self, force=False, timeout=120):
        with self._converting:
            pcap, hashes = self.pcap, self.hashes
            if not pcap or not hashes or not os.path.exists(pcap):
                return
            try:
                size = os.path.getsize(pcap)
            except OSError:
                return
            if size == 0 or (not force and size == self._last_size):
                return
            if captures.run_hcxpcapngtool(pcap, hashes, timeout,
                                          csv=self.csv) is not None:
                self._last_size = size


class PmkidHunter:
    def __init__(self, out_dir, scratch_dir, plan, settings):
        self.out_dir = out_dir
        self.scratch_dir = scratch_dir
        self.plan = plan
        self.settings = settings
        self.iface = plan.pmkid
        self.available = tool_available()
        self._tool_checked = 0.0
        self._lock = threading.Lock()
        self._dedicated = Capture(out_dir, 'pmkid', scratch_dir)
        self._sweep = Capture(out_dir, 'sweep', scratch_dir)
        self.security = {}
        self.patience = 1.0
        self.essids = ()
        self._last_written = 0
        self._last_heard = 0
        self._deaf_for = 0
        self._hushed = False
        self._targets = None
        self._ordered = None
        self._channels = ()
        self._dirty = False
        self._failures = 0
        self._last_spawn = 0.0
        self._convert_thread = None
        self._running = False
        self._sweeping = False
        self._held_reason = ''
        self._gave_up_at = 0.0
        self._act = threading.RLock()
        self._idle = threading.Event()

    @property
    def state(self):
        if not self.available:
            return UNAVAILABLE
        if self.iface:
            return DEDICATED if self._dedicated.alive() else HELD
        if self.settings.get('single_pmkid'):
            return SWEEP
        return OFF

    def describe(self):
        state = self.state
        if state == HELD and self._held_reason:
            return 'PMKID %s (%s)' % (self.iface, self._held_reason)
        return {
            UNAVAILABLE: 'PMKID off (no hcxdumptool)',
            DEDICATED: 'PMKID %s' % self.iface,
            HELD: 'PMKID %s (idle)' % self.iface,
            SWEEP: 'PMKID sweep',
            OFF: 'PMKID off',
        }[state]

    def start(self):
        self._running = True
        self._idle.clear()
        if not self.iface:
            return
        if not self._tool_ready():
            logging.warning('hcxdumptool missing - PMKID disabled for now')
            self._gave_up_at = time.monotonic()
            self.iface = None
            return
        if not interfaces.is_monitor(self.iface):
            logging.warning('%s not in monitor mode - PMKID stood down, '
                            'will retry', self.iface)
            self._gave_up_at = time.monotonic()
            self.iface = None
            return
        if self._hushed:
            self._held_reason = 'paused'
            return
        with self._act:
            with self._lock:
                empty = self._targets is not None and not self._targets
            if empty:
                logging.info('holding %s until targets are known', self.iface)
                self._held_reason = 'nothing in scope'
                return
            self._launch()

    def _tool_ready(self):
        if self.available:
            return True
        now = time.monotonic()
        if now - self._tool_checked < FALLBACK_RETRY:
            return False
        self._tool_checked = now
        self.available = tool_available()
        if self.available:
            logging.info('hcxdumptool is back, re-enabling PMKID')
        return self.available

    def _passive(self):
        try:
            return not self.settings.get('deauth_enabled')
        except Exception:
            return False

    def _bpf_path(self):
        return os.path.join(self.scratch_dir, '.pmkid.bpf')

    def _hold(self, reason):
        stopped = True
        if self._dedicated.alive():
            logging.info('holding %s: %s', self.iface, reason)
            stopped = self._dedicated.halt()
            self._dedicated.convert(force=True, timeout=ROTATE_CONVERT)
        if not stopped:
            logging.warning('%s is still capturing despite the hold', self.iface)
            return False
        captures.unmark_live(self._dedicated.pcap)
        self._held_reason = reason
        with self._lock:
            self._dirty = False
        return True

    def _launch(self):
        with self._lock:
            if not self._running:
                return False
            targets, ordered, chans = self._targets, self._ordered, self._channels
        if self._hushed:
            self._held_reason = 'paused'
            return False
        passive = self._passive()
        cmd = self._dedicated.build_cmd(self.iface, ordered, chans,
                                        self._bpf_path(), passive=passive,
                                        patience=self.patience,
                                        essids=self.essids)
        if cmd is None:
            self._hold('nothing in scope' if targets is not None and not targets
                       else 'target filter unavailable')
            return False
        self._held_reason = ''
        started = self._dedicated.spawn(cmd)
        if not started and chans:
            retry = self._dedicated.build_cmd(self.iface, ordered, chans,
                                              self._bpf_path(), passive=passive,
                                              with_channels=False,
                                              patience=self.patience,
                                              essids=self.essids)
            if retry is not None and self._dedicated.spawn(retry):
                logging.warning('the channel plan would not arm on %s, '
                                'hunting across all frequencies instead',
                                self.iface)
                started = True
        if not started:
            self._last_spawn = time.monotonic()
            self._failures += 1
            if self._failures >= MAX_LAUNCH_FAILURES:
                logging.warning('hcxdumptool failed %d times, falling back to '
                                'sweeps instead of %s', self._failures, self.iface)
                self.iface = None
                self._gave_up_at = time.monotonic()
            else:
                logging.warning('hcxdumptool did not start, will retry')
            return False
        self._failures = 0
        self._last_spawn = time.monotonic()
        self._last_written = 0
        self._deaf_for = 0
        with self._lock:
            self._dirty = False
        logging.info('PMKID hunting on %s (%s, channels %s)%s', self.iface,
                     'all' if targets is None else '%d targets' % len(targets),
                     channel_spec(chans) or 'all',
                     ' passive' if passive else '')
        with self._lock:
            running = self._convert_thread
            if running is None or not running.is_alive():
                thread = threading.Thread(target=self._convert_loop,
                                          name='pmkid-convert', daemon=True)
                self._convert_thread = thread
            else:
                thread = None
        if thread is not None:
            thread.start()
        return True

    def revive(self):
        wanted = getattr(self.plan, 'pmkid', None)
        if self.iface or not wanted or not self._gave_up_at:
            return False
        if time.monotonic() - self._gave_up_at < FALLBACK_RETRY:
            return False
        if not interfaces.exists(wanted) or not interfaces.is_monitor(wanted):
            self._gave_up_at = time.monotonic()
            return False
        logging.info('giving %s another chance at PMKID', wanted)
        self.iface = wanted
        self._failures = 0
        self._gave_up_at = 0.0
        self._held_reason = ''
        return True

    def retune(self, targets, channels=(), urgent=False):
        if not self._tool_ready() or not self.iface:
            return False
        ordered = None
        if targets is not None:
            ordered = []
            for target in targets:
                if not target:
                    continue
                mac = target.lower()
                if mac not in ordered:
                    ordered.append(mac)
        new = None if ordered is None else set(ordered)
        chans = tuple(sorted(set(channels or ())))
        with self._lock:
            unchanged = new == self._targets and chans == self._channels
            previous = self._targets
            self._targets = new
            self._ordered = ordered
            self._channels = chans
            running = self._running
            failures = self._failures
        if not running:
            return False
        empty = new is not None and not new
        with self._act:
            with self._lock:
                if not self._running:
                    return False
            if not self._dedicated.alive():
                if empty:
                    self._held_reason = 'nothing in scope'
                    return False
                if time.monotonic() - self._last_spawn < RESTART_COOLDOWN and failures:
                    return False
                logging.info('PMKID capture is not running, restarting it')
                return self._launch()
            if empty:
                self._hold('nothing in scope')
                return False
            if unchanged:
                return False
            with self._lock:
                self._dirty = True
            narrowed = new is not None and (previous is None
                                            or not new.issuperset(previous))
            if narrowed and urgent:
                return self._rotate()
            if time.monotonic() - self._last_spawn < RESTART_COOLDOWN:
                return False
            return self._rotate()

    def _deaf(self):
        if not self.iface or self._sweeping:
            return False
        written = self._dedicated.bytes_written()
        heard = conditions.rx_packets(self.iface)
        grew = written > self._last_written
        moved = heard is not None and heard > self._last_heard
        self._last_written = written
        if heard is not None:
            self._last_heard = heard
        if grew or not moved:
            self._deaf_for = 0
            return False
        self._deaf_for += 1
        return self._deaf_for >= DEAF_TICKS

    def restart(self, reason=''):
        with self._act:
            if not self._running or not self.iface or self._sweeping:
                return False
            if reason:
                logging.warning('restarting the PMKID capture on %s: %s',
                                self.iface, reason)
            self._dedicated.halt()
            self._dedicated.convert(force=True, timeout=ROTATE_CONVERT)
            interfaces.forget_monitor_cache()
            return self._launch()

    def _rotate(self):
        with self._act:
            if not self._dedicated.halt():
                logging.warning('not rotating: the old capture is still running')
                return False
            self._dedicated.convert(force=True, timeout=ROTATE_CONVERT)
            return self._launch()

    def _convert_loop(self):
        try:
            while self._running:
                try:
                    self._convert_tick()
                except Exception as e:
                    logging.warning('PMKID housekeeping failed: %s', e)
                if not self.iface:
                    break
        finally:
            with self._lock:
                if self._convert_thread is threading.current_thread():
                    self._convert_thread = None
            self._dedicated.convert(force=True, timeout=SHUTDOWN_CONVERT)

    def _convert_tick(self):
        self._idle.wait(CONVERT_INTERVAL)
        if not self._running:
            return
        self._dedicated.convert()
        self.absorb_security(self._dedicated.csv)
        with self._act:
            if not self._running:
                return
            with self._lock:
                dirty = self._dirty
            if self._hushed:
                return
            if self.iface and not interfaces.exists(self.iface):
                if self._dedicated.alive():
                    logging.warning('%s has gone away, stopping the capture '
                                    'that was holding it', self.iface)
                    self._dedicated.halt()
                    self._dedicated.convert(force=True, timeout=ROTATE_CONVERT)
                self._deaf_for = 0
                return
            if not self._dedicated.alive():
                if self.iface and not self._held_reason:
                    logging.warning('the PMKID capture stopped on its own, '
                                    'restarting it')
                    self._launch()
            elif self._deaf():
                logging.warning('the PMKID capture is running but has written '
                                'nothing while %s keeps receiving - its radio '
                                'was probably replaced, restarting it',
                                self.iface)
                self._deaf_for = 0
                self._rotate()
            elif dirty:
                self._rotate()
            elif time.monotonic() - self._last_spawn >= ROTATE_AFTER:
                logging.debug('rotating the PMKID capture file')
                self._rotate()

    def sweep(self, radio, targets, seconds, channels=(), should_stop=None):
        if (not self._running or not self._tool_ready() or self.iface
                or self._hushed or seconds <= 0):
            return False
        if not interfaces.is_monitor(radio.capture_iface):
            return False
        bpf = os.path.join(self.scratch_dir, '.sweep.bpf')
        cmd = self._sweep.build_cmd(radio.capture_iface, targets, channels, bpf,
                                    passive=self._passive(),
                                    patience=self.patience,
                                    essids=self.essids)
        if cmd is None:
            logging.warning('skipping the PMKID sweep: %s',
                            'nothing in scope' if targets is not None and not targets
                            else 'the target filter would not compile')
            return False
        with self._lock:
            if not self._running:
                return False
            self._sweeping = True
        radio.release_capture_radio()
        time.sleep(1)
        with self._lock:
            still_ours = self._sweeping and self._running
        if not still_ours:
            radio.enable_capture_radio()
            return False
        started = self._sweep.spawn(cmd)
        if not started:
            logging.warning('sweep would not start, restoring capture radio')
            self._finish_sweep(radio)
            return False
        logging.info('PMKID sweep on %s (%s, channels %s)', radio.capture_iface,
                     'all' if targets is None else '%d targets' % len(targets),
                     channel_spec(channels) or 'all')
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                break
            with self._lock:
                if not self._sweeping:
                    return True
            if not self._sweep.alive():
                logging.warning('the PMKID sweep stopped on its own, '
                                'giving the capture radio back early')
                break
            time.sleep(0.25)
        self._finish_sweep(radio)
        return True

    def hush(self):
        if not self._running or not self.iface:
            return False
        if self._sweeping or self._hushed:
            return False
        self._hushed = True
        with self._act:
            if not self._hold('paused'):
                self._hushed = False
                return False
        return True

    def unhush(self):
        if not self._hushed:
            return False
        with self._act:
            self._hushed = False
            if not self._running or not self.iface:
                return False
            self._held_reason = ''
            self._deaf_for = 0
            return self._launch()

    def discard_scratch(self):
        junk = [c.csv for c in (self._dedicated, self._sweep)]
        junk.append(self._bpf_path())
        junk.append(os.path.join(self.scratch_dir, '.sweep.bpf'))
        for path in junk:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                logging.debug('could not clear %s: %s', path, e)

    def absorb_security(self, path):
        if not path:
            return 0
        found = read_security(path)
        if not found:
            return 0
        with self._lock:
            for mac, info in found.items():
                if mac not in self.security and len(self.security) >= MAX_SECURITY:
                    self.security.pop(next(iter(self.security)))
                self.security[mac] = info
        return len(found)

    def guarded(self, bssid):
        with self._lock:
            info = self.security.get((bssid or '').lower())
        return needs_protection(info.get('akm') if info else ())

    def only_guarded(self, bssid):
        with self._lock:
            info = self.security.get((bssid or '').lower())
        return only_protection(info.get('akm') if info else ())

    def sweeping(self):
        with self._lock:
            return self._sweeping

    def _finish_sweep(self, radio, timeout=captures.CONVERT_TIMEOUT):
        with self._lock:
            if not self._sweeping:
                return
            self._sweeping = False
        self._sweep.halt()
        radio.enable_capture_radio()
        self._sweep.convert(force=True, timeout=timeout)
        self.absorb_security(self._sweep.csv)

    def stop(self, radio=None):
        with self._lock:
            self._running = False
            sweeping = self._sweeping
        self._idle.set()
        if sweeping and radio is not None:
            logging.info('stopping mid-sweep, restoring capture radio')
            self._finish_sweep(radio, SHUTDOWN_CONVERT)
        self._dedicated.halt()
        if not sweeping or radio is None:
            self._sweep.halt()
        if self._dedicated.alive():
            self._dedicated.halt()
        self._dedicated.convert(force=True, timeout=SHUTDOWN_CONVERT)
        self.absorb_security(self._dedicated.csv)
        self.absorb_security(self._sweep.csv)
        self.discard_scratch()
