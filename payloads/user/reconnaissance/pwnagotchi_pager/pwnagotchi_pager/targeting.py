import logging
import math
import threading
import time

from . import conditions, interfaces
from . import pineap as pineap_mod, pool as pool_mod
from . import scope as scope_mod, stats as stats_mod

MIN_ACTIVITY = 0.5
FRESH_SLACK = 10
MAX_CHURN_CUT = 0.8
MIN_DEAUTH_EVIDENCE = 30
MIN_SWEEP_EVIDENCE = 2
MAX_CONCENTRATION = 4
SHARED_CONCENTRATION = 2
ACTIVITY_WEIGHT = 25.0
ACTIVITY_CAP = 70
BAND_ACTIVITY_WEIGHT = 10.0
BAND_ACTIVITY_CAP = 30
PARTIAL_BONUS = 40
PMKID_STUBBORN_BONUS = 10
PMKID_GUARDED_BONUS = 15
PMKID_UNCRACKABLE_PENALTY = 25
PMKID_CHANNEL_RESERVE = 2
MAX_PMKID_CHANNELS = 14
PMKID_MIXED_CHANNELS = 8
PMKID_FAR_CHANNELS = 6
PATIENCE_NEAR = 1.0
PATIENCE_MIXED = 1.5
PATIENCE_FAR = 2.0
DEAF_CHANNEL_WEIGHT = 3
BAND_KEY = {pineap_mod.BAND_2G: '2', pineap_mod.BAND_5G: '5',
            pineap_mod.BAND_6G: '6'}
UNHEARD = -100
AIMABLE = ('clients', 'kin')
SPECULATIVE_TARGETS = 1
PINEAP_CALL = 0.25
ENGAGE_OVERHEAD = 0.4


def busy_score(ap, weight, cap):
    busy = ap.get('busy', 0)
    if busy <= 0:
        return 0.0
    return min(cap, weight * math.log10(1.0 + busy))


class Targeting:
    def __init__(self, world):
        self.world = world
        self._turns = threading.Lock()
        self._channel_turn = 0
        self._plan_size = 0
        self._sweep_turn = 0

    def tunable(self, channels, iface):
        phy = interfaces.phy_of(iface)
        if not phy or not interfaces.usable_channels(phy):
            return channels
        keep = []
        for channel, band in channels:
            key = BAND_KEY.get(band or pineap_mod.band_for_channel(channel))
            if not key:
                continue
            if channel in interfaces.usable_channels(phy, key):
                keep.append((channel, band))
        if len(keep) != len(channels):
            logging.debug('%d of %d channels are outside what %s can tune',
                          len(channels) - len(keep), len(channels), iface)
        return keep

    def pmkid_akm_weight(self, mac):
        if self.world.pmkid.only_guarded(mac):
            return -PMKID_UNCRACKABLE_PENALTY
        if self.world.pmkid.guarded(mac):
            return PMKID_GUARDED_BONUS
        return 0

    def pmkid_order(self, targets):
        if targets is None:
            return None
        ranked = [ap for ap in self.world.access_points
                  if ap.get('mac', '').lower() in targets]
        ranked.sort(key=lambda ap: ap.get('rssi', UNHEARD)
                    + (PMKID_STUBBORN_BONUS if self.stubborn(ap) else 0)
                    + self.pmkid_akm_weight(ap.get('mac', '')),
                    reverse=True)
        ordered = [ap['mac'].lower() for ap in ranked]
        for mac in sorted(targets):
            if mac not in ordered:
                ordered.append(mac)
        return ordered

    def pmkid_patience(self):
        if self.world.env.reach == conditions.FAR:
            return PATIENCE_FAR
        if self.world.env.reach == conditions.NEAR:
            return PATIENCE_NEAR
        return PATIENCE_MIXED

    def pmkid_channel_room(self):
        if self.world.env.reach == conditions.FAR:
            return PMKID_FAR_CHANNELS
        if self.world.env.reach == conditions.MIXED:
            return PMKID_MIXED_CHANNELS
        return MAX_PMKID_CHANNELS

    def hot_channels(self):
        weights = {}
        widest = self.pmkid_channel_room()
        only = self.world.settings.channels
        for ap in self.world.access_points:
            channel = ap.get('channel')
            if not channel or (only and channel not in only):
                continue
            if not self.world.scope.targetable(ap):
                continue
            key = (channel, ap.get('band', pineap_mod.NO_BAND))
            worth = DEAF_CHANNEL_WEIGHT if self.cannot_inject(ap) else 1
            weights[key] = weights.get(key, 0) + worth
        if len(weights) <= widest:
            return sorted(weights)
        traffic = self.world.pineap.traffic()
        typical = pineap_mod.typical_load(traffic)
        ranked = sorted(weights.items(),
                        key=lambda kv: (-pineap_mod.channel_score(
                            kv[1], traffic.get(kv[0], 0), typical=typical)
                            * self.channel_bias(kv[0][0], kv[0][1]), kv[0]))
        reserve = max(PMKID_CHANNEL_RESERVE, widest // 3)
        room = max(1, widest - reserve)
        keep = [k for k, _ in ranked[:room]]
        rest = [k for k, _ in ranked[room:]]
        if rest:
            take = min(widest - len(keep), len(rest))
            with self._turns:
                start = self._sweep_turn % len(rest)
                self._sweep_turn = (start + take) % len(rest)
            keep.extend(rest[(start + i) % len(rest)] for i in range(take))
        return sorted(set(keep))

    def priority(self, ap):
        mac = ap.get('mac', '').lower()
        score = 0.0
        if ap.get('clients'):
            score += 100 + 10 * len(ap['clients'])
        score += 8 * min(6, ap.get('channel_clients', 0))
        score += max(0, ap.get('rssi', UNHEARD) + 100)
        score += busy_score(ap, ACTIVITY_WEIGHT, ACTIVITY_CAP)
        if mac in self.world.captures.partials:
            score += PARTIAL_BONUS
        if ap.get('hidden'):
            score -= 15
        score -= self.world.history.penalty(mac)
        return score

    def cannot_inject(self, ap):
        return self.transmit_blocked(ap) or self.world.history.refuses_injection(ap)

    def transmit_blocked(self, ap):
        channel = ap.get('channel', 0)
        band = BAND_KEY.get(ap.get('band') or '')
        if not channel or not band:
            return False
        phy = interfaces.phy_of(self.world.plan.capture)
        if not phy or not interfaces.usable_channels(phy):
            return False
        return channel not in interfaces.transmit_channels(phy, band)

    def rssi_floor(self, aps):
        base = self.world.settings.tune('min_attack_rssi')
        span = self.world.settings.tune('rssi_adapt')
        if span <= 0:
            return base
        rich = self.world.settings.tune('rssi_rich_targets')
        worth = [ap for ap in aps if self.world.scope.targetable(ap)]
        strong = sum(1 for ap in worth if ap.get('rssi', UNHEARD) >= base + span)
        if strong >= rich:
            return base + span
        if self.world.epoch.inactive_for >= 2 or len(worth) < rich:
            return base - span
        return base

    def channel_bias(self, channel, band=None):
        return self.world.stats.bias(channel, band)

    def effort(self, ap):
        if not self.world.settings.get('deauth_enabled'):
            return 0
        if self.cannot_inject(ap):
            return 0
        if not self.has_clients(ap):
            return 0
        tries = self.world.history.tiredness(ap.get('mac', '').lower())
        if tries <= 0:
            return 0
        return min(self.world.settings.tune('max_effort'), tries)

    def attack_stations(self, ap):
        floor = self.world.settings.tune('targeted_rssi')
        heard = ap.get('heard') or {}
        anywhere = ap.get('rssi', UNHEARD) >= floor

        def reachable(macs, limit=None):
            macs = [m for m in (macs or ()) if m
                    and (anywhere or heard.get(m, UNHEARD) >= floor)]
            macs.sort(key=lambda m: -heard.get(m, UNHEARD))
            return macs[:limit] if limit else macs

        stations = reachable(ap.get('clients'))
        if stations:
            return stations, 'clients'
        spare = self.world.settings.tune('nearby_clients')
        kin = reachable(ap.get('kin'), spare)
        if kin:
            return kin, 'kin'
        nearby = reachable(ap.get('nearby'), spare)
        return (nearby, 'nearby') if nearby else ([], '')

    def aim_limit(self, source, push=0):
        return (3 + push) if source in AIMABLE else SPECULATIVE_TARGETS

    def attack_seconds(self, ap, push=0):
        stations, source = self.attack_stations(ap)
        rounds = min(len(stations), self.aim_limit(source, push)) or 1
        if source not in AIMABLE:
            rounds += 1
        bursts = self.burst_count(not self.hearable(ap),
                                  self.world.env.noise(ap.get('channel'),
                                                 ap.get('band')))
        gap = self.world.settings.tune('deauth_burst_gap')
        each = bursts * (PINEAP_CALL + gap) + self.world.settings.tune('throttle_deauth')
        opening = ENGAGE_OVERHEAD + PINEAP_CALL + self.world.settings.tune('throttle_assoc')
        return opening + rounds * each

    def burst_count(self, faint=False, noise=None):
        extra = 1 if faint else 0
        if noise == conditions.LOUD:
            extra += self.world.settings.tune('loud_channel_bursts')
        elif noise == conditions.BUSY:
            extra += 1
        return self.world.settings.tune('deauth_bursts') + extra

    def stubborn(self, ap):
        mac = ap.get('mac', '').lower()
        return self.world.history.is_deauth_dead(mac) or self.world.history.worn_out(mac)

    def has_clients(self, ap):
        return bool(ap.get('clients') or ap.get('kin') or ap.get('nearby')
                    or ap.get('channel_clients')
                    or ap.get('busy', 0) >= MIN_ACTIVITY)

    def churn_cut(self):
        return 1.0 - min(MAX_CHURN_CUT, max(0.0, self.world.mobility.churn))

    def still_there(self, ap):
        seen = ap.get('last_seen')
        if not seen:
            return True
        window = self.world.mobility.scale('recon_scale',
                                     self.world.settings.tune('recon_time'))
        window = max(self.world.settings.tune('min_recon_time'),
                     window * self.churn_cut())
        return time.time() - seen <= window + FRESH_SLACK

    def hearable(self, ap):
        return ap.get('rssi', UNHEARD) >= self.world.settings.tune('handshake_rssi')

    def deauth_worth_it(self, ap):
        if self.cannot_inject(ap):
            return False
        mac = ap.get('mac', '').lower()
        covered = self.hunting_with()[1]
        if self.world.pmkid.only_guarded(mac):
            logging.debug('%s only offers WPA3, where deauth cannot land', mac)
            return False
        if self.world.history.is_deauth_dead(mac) and covered:
            logging.debug('%s never answers a deauth, leaving it to PMKID', mac)
            return False
        if not self.has_clients(ap) and covered:
            logging.debug('%s has nobody attached to knock off, '
                          'leaving it to PMKID', mac)
            return False
        if not self.hearable(ap) and covered:
            logging.debug('%s is too faint for a handshake (%s dBm), '
                          'leaving it to PMKID', ap.get('mac'), ap.get('rssi'))
            return False
        return not self.world.history.worn_out(ap.get('mac', '').lower())

    def hunting_with(self):
        deauth = bool(self.world.settings.get('deauth_enabled'))
        pmkid = bool(self.world.pmkid.available
                     and (self.world.pmkid.iface or self.world.settings.get('single_pmkid')))
        return deauth, pmkid

    def band_evidence(self, ap):
        if self.cannot_inject(ap):
            return -2
        if not self.deauth_worth_it(ap):
            return -1
        return (len(ap.get('clients') or ()) * 4
                + busy_score(ap, BAND_ACTIVITY_WEIGHT, BAND_ACTIVITY_CAP)
                + min(6, ap.get('channel_clients', 0))
                + (2 if self.hearable(ap) else 0))

    def focus_bands(self, attackable):
        if not self.world.scope.cover_siblings or len(attackable) < 2:
            return attackable
        families = {}
        for ap in attackable:
            key = (scope_mod.group_key(ap.get('mac', '')),
                   str(ap.get('hostname') or '').lower())
            if not key[0]:
                key = (ap.get('mac', '').lower(), key[1])
            families.setdefault(key, []).append(ap)
        kept = []
        folded = 0
        for members in families.values():
            if len(members) == 1:
                kept.append(members[0])
                continue
            scored = [(self.band_evidence(ap), ap) for ap in members]
            best = max(mark for mark, _ in scored)
            tied = [ap for mark, ap in scored if mark == best]
            chosen = tied[self.world.epoch.number % len(tied)]
            kept.append(chosen)
            folded += len(members) - 1
            logging.debug('%s answers on %d band(s), working ch %s this epoch',
                          chosen.get('hostname') or chosen.get('mac'),
                          len(members), chosen.get('channel'))
        if folded:
            logging.info('folded %d sibling radio(s) into the band their '
                         'clients are actually on', folded)
        return kept

    def deauth_looks_futile(self):
        return bool(self.world.stats.captures[stats_mod.PMKID]
                    and self.world.stats.dry[stats_mod.DEAUTH] >= MIN_DEAUTH_EVIDENCE)

    def targets_room(self, channels=0):
        crowd = self.world.settings.tune('max_targets_per_channel')
        if self.world.mobility.moving:
            crowd = max(1, int(round(crowd * self.churn_cut())))
        if self.deauth_looks_futile():
            crowd = max(1, crowd // 2)
        if channels < 1:
            return crowd
        deepest = (MAX_CONCENTRATION if self.world.pmkid.iface
                   else SHARED_CONCENTRATION)
        share = (crowd * self.channel_room()) // channels
        return max(crowd, min(crowd * deepest, share))

    def channel_room(self):
        room = self.world.settings.tune('max_channels_per_epoch')
        if self.world.env.density == conditions.THIN:
            return max(room, 64)
        if self.world.mobility.moving:
            room = max(1, int(round(room * self.churn_cut())))
        if self.deauth_looks_futile():
            return max(1, room // 2)
        if (self.world.stats.captures[stats_mod.DEAUTH]
                and not self.world.stats.captures[stats_mod.PMKID]):
            return room + 2
        return room

    def advance_channel_turn(self, visited):
        with self._turns:
            if not self._plan_size or visited <= 0:
                return
            self._channel_turn = (self._channel_turn + visited) % self._plan_size

    def channel_plan(self, attackable, only):
        groups = self.world.pineap.by_channel(attackable, only, weigh=self.channel_bias)
        room = self.channel_room()
        self._plan_size = len(groups)
        if len(groups) <= room:
            self._channel_turn = 0
            return groups
        turn = self._channel_turn % len(groups)
        rotated = groups[turn:] + groups[:turn]
        logging.debug('%d channels are worth visiting, taking %d this epoch '
                      '(from %d)', len(groups), room, turn)
        return rotated[:room]

    def sweep_plan(self):
        pinned = self.world.settings.configured
        every = max(1, self.world.settings.tune('pmkid_sweep_every_epochs'))
        seconds = self.world.settings.tune('pmkid_sweep_secs')
        low = self.world.settings.tune('pmkid_sweep_min_secs')
        high = self.world.settings.tune('pmkid_sweep_max_secs')
        if 'pmkid_sweep_secs' in pinned:
            low = min(low, seconds)
            high = max(high, seconds)

        fixed_every = 'pmkid_sweep_every_epochs' in pinned
        fixed_secs = 'pmkid_sweep_secs' in pinned
        clients = any(ap.get('clients') for ap in self.world.access_points
                      if self.world.scope.targetable(ap))
        if not fixed_secs and self.world.env.reach == conditions.FAR:
            seconds = high
        elif not fixed_secs and self.world.env.reach == conditions.NEAR \
                and self.world.env.density == conditions.CROWDED:
            seconds = max(low, seconds * 0.8)
        if not clients:
            if not fixed_every:
                every = 1
            if not fixed_secs:
                seconds = high
        elif (self.world.epoch.inactive_for >= 2
              or self.world.stats.dry[stats_mod.DEAUTH] >= MIN_DEAUTH_EVIDENCE):
            if not fixed_every:
                every = 1
        elif (self.world.stats.dry[stats_mod.PMKID] >= MIN_SWEEP_EVIDENCE
              and self.world.stats.captures[stats_mod.DEAUTH] > 1):
            if not fixed_every:
                every = min(3, every + 1)
            if not fixed_secs:
                seconds = max(low, seconds * 0.75)
        return every, int(max(low, min(high, seconds)))

    def pool_channel(self):
        two_ghz = pineap_mod.BAND_2G
        usable = interfaces.transmit_channels(pool_mod.open_ap_phy(), '2')
        traffic = self.world.pineap.traffic()
        busy = [(load, ch) for (ch, band), load in traffic.items()
                if band == two_ghz and load and (not usable or ch in usable)]
        if busy:
            return max(busy)[1]
        counts = {}
        for ap in self.world.access_points:
            ch = ap.get('channel', 0)
            if not ch or ap.get('band') != two_ghz:
                continue
            if usable and ch not in usable:
                continue
            counts[ch] = counts.get(ch, 0) + 1
        return max(counts, key=counts.get) if counts else 0
