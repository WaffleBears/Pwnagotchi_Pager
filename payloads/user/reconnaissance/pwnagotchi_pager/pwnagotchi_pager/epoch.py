import logging
import threading
import time

from . import system

class Epoch:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self.number = 0
        self.started_at = time.monotonic()
        self.duration = 0.0
        self.inactive_for = 0
        self.active_for = 0
        self.blind_for = 0
        self.sad_for = 0
        self.bored_for = 0
        self.data = {}
        self._reset_counters()

    def _reset_counters(self):
        self.did_deauth = False
        self.did_associate = False
        self.did_handshakes = False
        self.any_activity = False
        self.num_deauths = 0
        self.num_assocs = 0
        self.num_shakes = 0
        self.num_missed = 0
        self.num_hops = 0
        self.num_slept = 0

    def track(self, deauth=False, assoc=False, handshake=False, hop=False,
              sleep=False, miss=False, inc=1):
        with self._lock:
            if deauth:
                self.num_deauths += inc
                self.did_deauth = True
                self.any_activity = True
            if assoc:
                self.num_assocs += inc
                self.did_associate = True
                self.any_activity = True
            if handshake:
                self.num_shakes += inc
                self.did_handshakes = True
            if hop:
                self.num_hops += inc
                self.did_deauth = False
                self.did_associate = False
            if miss:
                self.num_missed += inc
            if sleep:
                self.num_slept += inc

    def observe(self, aps):
        with self._lock:
            self.blind_for = 0 if aps else self.blind_for + 1

    def blind_alarm(self, limit):
        with self._lock:
            if self.blind_for < limit:
                return 0
            count, self.blind_for = self.blind_for, 0
            return count

    @property
    def stale(self):
        return self.num_missed > self.settings.tune('max_misses_for_recon')

    def reward(self):
        return round(self.num_shakes * 10.0 + self.num_deauths + self.num_assocs
                     - self.inactive_for, 2)

    def advance(self):
        bored = self.settings.tune('bored_epochs')
        sad = self.settings.tune('sad_epochs')
        with self._lock:
            if not self.any_activity and not self.did_handshakes:
                self.inactive_for += 1
                self.active_for = 0
            else:
                self.active_for += 1
                self.inactive_for = 0
                self.sad_for = 0
                self.bored_for = 0

            if self.inactive_for >= sad:
                self.bored_for = 0
                self.sad_for += 1
            elif self.inactive_for >= bored:
                self.sad_for = 0
                self.bored_for += 1
            else:
                self.sad_for = 0
                self.bored_for = 0

            now = time.monotonic()
            self.duration = now - self.started_at
            self.data = {
                'epoch': self.number,
                'duration': self.duration,
                'slept': self.num_slept,
                'blind_for': self.blind_for,
                'inactive_for': self.inactive_for,
                'active_for': self.active_for,
                'missed': self.num_missed,
                'hops': self.num_hops,
                'deauths': self.num_deauths,
                'associations': self.num_assocs,
                'handshakes': self.num_shakes,
                'reward': self.reward(),
                'cpu_load': system.cpu_load(),
                'mem_usage': system.mem_usage(),
                'temperature': system.temperature(),
            }
            logging.info('[epoch %d] %s slept=%s blind=%d bored=%d sad=%d '
                         'inactive=%d active=%d hops=%d missed=%d deauths=%d '
                         'assocs=%d shakes=%d reward=%.1f load=%.2f mem=%.0f%% temp=%.0fC',
                         self.number, system.secs_to_hhmmss(self.duration),
                         system.secs_to_hhmmss(self.num_slept), self.blind_for,
                         self.bored_for, self.sad_for, self.inactive_for,
                         self.active_for, self.num_hops, self.num_missed,
                         self.num_deauths, self.num_assocs, self.num_shakes,
                         self.data['reward'], self.data['cpu_load'],
                         self.data['mem_usage'], self.data['temperature'])
            snapshot = dict(self.data)
            self.number += 1
            self.started_at = now
            self._reset_counters()
            return snapshot


STARTING = 'starting'
NORMAL = 'normal'
BORED = 'bored'
SAD = 'sad'
ANGRY = 'angry'
LONELY = 'lonely'
EXCITED = 'excited'
MOTIVATED = 'motivated'
DEMOTIVATED = 'demotivated'


class Mood:
    def __init__(self, epoch, settings, screen):
        self.epoch = epoch
        self.settings = settings
        self.screen = screen
        self.current = STARTING

    def _enter(self, name):
        self.current = name
        getattr(self.screen, 'on_' + name)()

    def on_miss(self, who):
        logging.info('%s is out of range', who)
        self.epoch.track(miss=True)
        self.screen.on_miss(who)

    def on_error(self, who, err):
        if 'unknown BSSID' in str(err):
            self.on_miss(who)
        else:
            logging.error('%s: %s', who, err)

    def settle(self, snapshot):
        missed = snapshot['missed']
        reward = snapshot['reward']
        max_misses = self.settings.tune('max_misses_for_recon')
        was_stale = missed > max_misses
        sad_epochs = self.settings.tune('sad_epochs')
        excited_epochs = self.settings.tune('excited_epochs')

        if was_stale:
            if missed / max(1, max_misses) >= 2.0:
                self._enter(ANGRY)
            else:
                logging.warning('missed %d interactions', missed)
                self._enter(LONELY)
        elif self.epoch.sad_for:
            if self.epoch.inactive_for / max(1, sad_epochs) >= 2.0:
                self._enter(ANGRY)
            else:
                self._enter(SAD)
        elif self.epoch.bored_for:
            self._enter(BORED)
        elif snapshot['handshakes'] > 0:
            self.screen.on_motivated(reward)
            self.current = MOTIVATED
        elif self.epoch.active_for >= excited_epochs:
            self._enter(EXCITED)
        elif snapshot['deauths'] or snapshot['associations']:
            if reward >= 5:
                self.screen.on_motivated(reward)
                self.current = MOTIVATED
            elif reward < 0:
                self.screen.on_demotivated(reward)
                self.current = DEMOTIVATED
            else:
                self.current = NORMAL
        else:
            self.current = NORMAL

        blind = self.epoch.blind_alarm(self.settings.tune('max_blind_epochs'))
        if blind:
            logging.critical('%d epochs with no visible access points', blind)
