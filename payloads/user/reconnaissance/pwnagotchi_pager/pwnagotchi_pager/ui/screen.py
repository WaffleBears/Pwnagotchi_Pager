import logging
import random
import threading
import time

from .. import pineap, system, voice
from . import look
from .look import (CHIP_PAD, MEDIUM_SIZE, MICRO_SIZE, MONO, PAD,
                     SMALL_SIZE, UI, UI_BOLD, Layout, wrap)

LOOK_FLIP = 1.6
MIN_WAIT_STEP = 0.5
MAX_WAIT_STEPS = 10
PHRASE_LIFE = 12.0
PHRASE_GAP = 1.0
MAX_RENDER_BACKOFF = 2.0


class Screen:
    def __init__(self, display, settings):
        self.display = display
        self.settings = settings
        self.layout = Layout(display)
        self.agent = None
        self._lock = threading.RLock()
        self._paint = threading.RLock()
        self._dirty = threading.Event()
        self._regions = set()
        self._phrase = None
        self._phrase_kind = None
        self._phrase_at = 0.0
        self._phrase_end = 0.0
        self._repaint_all = True
        self._stop = threading.Event()
        self._frozen = False
        self._render_held = 0
        self._held = None
        self._menu = None
        self._menus = []
        self._last_activity = time.monotonic()
        self._dimmed = False
        self._render_thread = None
        self._clock_thread = None

        self.fields = {
            'channel': '--',
            'aps': '0',
            'uptime': '00:00:00',
            'status': voice.default(),
            'face': look.SLEEP,
            'name': 'pwnagotchi',
            'pwnd': '0/0',
            'last': '',
            'battery': '?',
            'hint': '',
        }
        self._brightness = settings.get('brightness')
        self._theme_name = settings.get('theme')
        display.brightness(self._brightness)
        self._lit = self._brightness
        settings.on_change(self._on_settings)

    def _on_settings(self, settings=None):
        wanted = self.settings.get('brightness')
        theme_name = self.settings.get('theme')
        if theme_name != self._theme_name:
            self._theme_name = theme_name
            self.invalidate()
        if wanted == self._brightness:
            return
        self._brightness = wanted
        self._apply_brightness()

    def _apply_brightness(self):
        level = self._brightness
        if self._dimmed:
            level = min(self.settings.get('auto_dim_level'), level)
        if level == self._lit:
            return
        self._lit = level
        self.display.brightness(level)

    def bind(self, agent):
        self.agent = agent
        self.fields['hint'] = agent.plan.describe()

    def attach_menu(self, menu):
        with self._lock:
            self._menus.append(menu)
            self._menu = menu
        self.invalidate()

    def detach_menu(self):
        with self._lock:
            if self._menus:
                self._menus.pop()
            self._menu = self._menus[-1] if self._menus else None
        self.invalidate()

    def start(self):
        self._stop.clear()
        self._render_thread = threading.Thread(target=self._render_loop,
                                               name='render', daemon=True)
        self._render_thread.start()
        self._clock_thread = threading.Thread(target=self._clock_loop,
                                              name='clock', daemon=True)
        self._clock_thread.start()

    def stop(self):
        self._stop.set()
        self._dirty.set()
        try:
            self.settings.off_change(self._on_settings)
        except Exception as e:
            logging.debug('screen listener: %s', e)
        for t in (self._render_thread, self._clock_thread):
            if t and t.is_alive():
                t.join(timeout=2)

    def wake(self):
        self._dirty.set()

    def invalidate(self, region=None):
        with self._lock:
            if region is None:
                self._repaint_all = True
            else:
                self._regions.add(region)
            menu = self._menu
        if menu is not None:
            menu.forget()
        self._dirty.set()

    def set(self, key, value):
        with self._lock:
            if self.fields.get(key) == value:
                return
            self.fields[key] = value
            if key in self.layout.regions:
                self._regions.add(key)
            else:
                self._repaint_all = True
            menu = self._menu
        if menu is None:
            self._dirty.set()

    def set_status(self, text):
        if self._holding():
            return
        self.set('status', text)

    def set_channel(self, channel, band=None):
        if not channel:
            self.set('channel', '--')
        else:
            label = pineap.channel_band(channel, band)
            self.set('channel', ('%d %s' % (channel, label)).strip())

    def _face(self, face):
        return random.choice(face) if isinstance(face, list) else face

    def _band_text(self, d, th, x, y, h, label, value, colour=None,
                   font=MONO, size=SMALL_SIZE):
        lay = self.layout
        if label:
            top = y + (h - lay.micro_h) // 2
            d.draw_ttf(x, top, label, th['dim'], UI_BOLD, MICRO_SIZE)
            x += (d.ttf_width(label, UI_BOLD, MICRO_SIZE) or 0) + 6
        top = y + (h - lay.mono_h) // 2
        d.draw_ttf(x, top, value, colour or th['text'], font, size)

    def _draw_field(self, d, th, key):
        lay = self.layout
        value = self.fields[key]
        if key == 'name':
            top = lay.header_y + (lay.header_h - lay.bold_h) // 2
            d.draw_ttf(PAD, top, value, th['accent'], UI_BOLD, SMALL_SIZE)
        elif key == 'uptime':
            x, y, w, h = lay.regions['uptime']
            width = d.ttf_width(value, MONO, SMALL_SIZE) or 0
            self._band_text(d, th, x + w - width - 12, y, h, '', value,
                            th['label'])
        elif key == 'battery':
            x, y, w, h = lay.regions['battery']
            digits = value.rstrip('+')
            text = digits + '%' + value[len(digits):] if digits.isdigit() else value
            width = d.ttf_width(text, MONO, SMALL_SIZE) or 0
            colour = th['text']
            if value.rstrip('+').isdigit():
                level = int(value.rstrip('+'))
                colour = th['off'] if level <= 15 else (
                    th['warning'] if level <= 30 else th['on'])
            self._band_text(d, th, x + w - width - PAD, y, h, '', text, colour)
        elif key == 'aps':
            x, y, w, h = lay.regions['aps']
            self._band_text(d, th, x + PAD, y, h, 'APS', value, th['text'])
        elif key == 'pwnd':
            x, y, w, h = lay.regions['pwnd']
            self._band_text(d, th, x, y, h, 'GOT', value, th['accent'])
        elif key == 'last':
            if not value:
                return
            x, y, w, h = lay.regions['last']
            budget = w - PAD
            while value and (d.ttf_width(value, UI_BOLD, SMALL_SIZE) or 0) > budget:
                value = value[:-1]
            width = d.ttf_width(value, UI_BOLD, SMALL_SIZE) or 0
            top = y + (h - lay.bold_h) // 2
            d.draw_ttf(x + w - width - PAD, top, value,
                       th['on'], UI_BOLD, SMALL_SIZE)
        elif key == 'face':
            width = d.ttf_width(value, MONO, lay.face_size) or 0
            d.draw_ttf(max(PAD, (lay.split_x - width) // 2), lay.face_y,
                       value, th['face'], MONO, lay.face_size)
        elif key == 'channel':
            self._draw_chip(d, th, value)
        elif key == 'status':
            lines = wrap(d, value, lay.right_w)
            if len(lines) > lay.status_rows:
                lines = lines[:lay.status_rows]
                if lines:
                    lines[-1] = lines[-1].rstrip(' .,;:') + '...'
            band = lay.hint_y - lay.body_top
            used = len(lines) * lay.status_line_h
            y = lay.body_top + max(0, (band - used) // 2)
            for line in lines:
                d.draw_ttf(lay.right_x, y, line, th['text'], UI, MEDIUM_SIZE)
                y += lay.status_line_h
        elif key == 'hint':
            if value:
                budget = lay.regions['hint'][2] - PAD
                if (d.ttf_width(value, UI_BOLD, MICRO_SIZE) or 0) > budget:
                    while value and (d.ttf_width(value + '~', UI_BOLD,
                                                 MICRO_SIZE) or 0) > budget:
                        value = value[:-1]
                    value = value.rstrip(' -') + '~'
                d.draw_ttf(lay.right_x, lay.hint_y, value, th['dim'],
                           UI_BOLD, MICRO_SIZE)

    def _draw_chip(self, d, th, value):
        lay = self.layout
        label = 'CH'
        lw = d.ttf_width(label, UI_BOLD, MICRO_SIZE) or 0
        vw = d.ttf_width(value, MONO, SMALL_SIZE) or 0
        width = lw + vw + CHIP_PAD * 2 + 6
        x = max(PAD, (lay.split_x - width) // 2)
        d.fill_rect(x, lay.chip_y, width, lay.chip_h, th['chip'])
        d.hline(x, lay.chip_y, width, th['line'])
        d.hline(x, lay.chip_y + lay.chip_h - 1, width, th['line'])
        top = lay.chip_y + (lay.chip_h - lay.micro_h) // 2
        d.draw_ttf(x + CHIP_PAD, top, label, th['dim'], UI_BOLD, MICRO_SIZE)
        top = lay.chip_y + (lay.chip_h - lay.mono_h) // 2
        d.draw_ttf(x + CHIP_PAD + lw + 6, top, value, th['accent'],
                   MONO, SMALL_SIZE)

    def _holding(self):
        return bool(self._held) and time.monotonic() < self._held

    def _show(self, face, status, hold=0.0, override=False):
        if self._holding() and not override:
            return
        with self._lock:
            face = self._face(face)
            moved = set()
            if self.fields['face'] != face:
                self.fields['face'] = face
                moved.add('face')
            if self.fields['status'] != status:
                self.fields['status'] = status
                moved.add('status')
            if moved:
                self._regions.update(moved)
            self._held = time.monotonic() + hold if hold else 0.0
            menu = self._menu
        if moved and menu is None:
            self._dirty.set()

    def on_starting(self):
        self._show(look.AWAKE, voice.on_starting(), override=True)

    def on_normal(self):
        self._show(look.AWAKE, voice.on_normal())

    def on_idle(self, reason):
        if self._holding():
            return
        if reason == 'radio not answering':
            self._show(look.BROKEN, voice.on_radio_trouble(), hold=6.0)
        elif reason == 'captured':
            self._show(look.COOL, voice.on_all_captured(), hold=6.0)
        elif reason == 'out of scope':
            self._show(look.BORED, voice.on_out_of_scope(), hold=6.0)
        elif reason == 'channels filtered':
            self._show(look.BORED, voice.on_channels_filtered(), hold=6.0)
        elif reason == 'too far':
            self._show(look.LOOK_R, voice.on_too_far(), hold=6.0)
        elif reason == 'moved on':
            self._show(look.LOOK_L, voice.on_moved_on(), hold=6.0)
        else:
            self._show(look.LONELY, voice.on_nothing_here(), hold=6.0)

    def on_assoc(self, ap):
        who = ap.get('hostname') or ap.get('mac', '')
        self._show(look.INTENSE, voice.on_assoc(who))

    def on_deauth(self, who, targeted=False):
        self._show(look.COOL, voice.on_deauth(who, targeted))

    def on_miss(self, who):
        self._show(look.SAD, voice.on_miss(who))

    def on_handshakes(self, count, name=''):
        self._show(look.HAPPY, voice.on_handshakes(count, name), hold=6.0,
                   override=True)

    def on_bored(self):
        self._show(look.BORED, voice.on_bored(), hold=2.0, override=True)

    def on_sad(self):
        self._show(look.SAD, voice.on_sad(), hold=2.0, override=True)

    def on_angry(self):
        self._show(look.ANGRY, voice.on_angry(), hold=2.0, override=True)

    def on_lonely(self):
        self._show(look.LONELY, voice.on_lonely(), hold=2.0, override=True)

    def on_excited(self):
        self._show(look.EXCITED, voice.on_excited(), hold=2.0, override=True)

    def on_motivated(self, reward=0):
        self._show(look.MOTIVATED, voice.on_motivated(reward), hold=2.0, override=True)

    def on_demotivated(self, reward=0):
        self._show(look.DEMOTIVATED, voice.on_demotivated(reward), hold=2.0,
                   override=True)

    def on_shutdown(self):
        self._show(look.SLEEP, voice.on_shutdown(), override=True)
        with self._paint:
            self._render(force=True)
            self._frozen = True

    def freeze(self, title, subtitle='', sticky=True):
        th = look.get(self.settings.get('theme'))
        with self._paint:
            self._frozen = True
            with self.display.frame() as d:
                d.clear(th['bg'])
                d.draw_ttf_centered(88, title, th['text'], UI_BOLD, 22.0)
                if subtitle:
                    d.draw_ttf_centered(122, subtitle, th['dim'], UI, SMALL_SIZE)
            self._frozen = sticky

    def unfreeze(self):
        self._frozen = False
        self.invalidate()

    def look_face(self):
        return (look.LOOK_L if int(time.monotonic() / LOOK_FLIP) % 2
                else look.LOOK_R)

    def _steady_phrase(self, kind, chooser):
        now = time.monotonic()
        if (self._phrase is None or self._phrase_kind != kind
                or now - self._phrase_end > PHRASE_GAP
                or now - self._phrase_at > PHRASE_LIFE):
            self._phrase = chooser()
            self._phrase_kind = kind
            self._phrase_at = now
        return self._phrase

    def wait(self, seconds, sleeping=True, countdown=None, settle=True):
        agent = self.agent
        steps = max(1, min(MAX_WAIT_STEPS, int(seconds / MIN_WAIT_STEP)))
        part = seconds / float(steps)
        shown = seconds if countdown is None else countdown
        if sleeping:
            phrase = (self._steady_phrase('nap', voice.napping_phrase)
                      if seconds > 1 else None)
            settled = voice.on_awakening()
        else:
            phrase = self._steady_phrase('wait', voice.waiting_phrase)
            settled = None
        for step in range(steps):
            if agent and agent.should_stop():
                return
            left = shown - step * part
            if sleeping:
                self._show(look.SLEEP if seconds > 1 else look.AWAKE,
                           voice.count_down(phrase, left) if phrase else settled)
            else:
                self._show(self.look_face(), voice.count_down(phrase, left))
            if agent is not None:
                if not agent.sleep(part):
                    return
            else:
                time.sleep(part)
        self._phrase_end = time.monotonic()
        if settle:
            self.on_normal()

    def update_stats(self, agent):
        total = len(agent.seen_points) or len(agent.access_points)
        channel = agent.pineap.current_channel
        band = agent.pineap.current_band
        if channel:
            here = sum(1 for ap in agent.access_points
                       if ap.get('channel') == channel
                       and (not band or ap.get('band', '') == band))
            self.set('aps', '%d/%d' % (here, total))
        else:
            self.set('aps', '%d' % total)
        session, total = agent.pwnd_counts()
        self.set('pwnd', '%d/%d' % (session, total))
        self.set('last', agent.last_capture())
        level = system.battery()
        charging = system.battery_charging()
        if level is None:
            self.set('battery', '?')
        else:
            self.set('battery', '%d%s' % (level, '+' if charging else ''))
        self.set('hint', agent.status_line())

    def touch(self):
        self._last_activity = time.monotonic()
        if self._dimmed:
            self._dimmed = False
            self._apply_brightness()
            return True
        return False

    def _check_dim(self):
        if self._dimmed:
            return
        timeout = self.settings.get('auto_dim')
        if timeout and time.monotonic() - self._last_activity >= timeout:
            self._dimmed = True
            self._apply_brightness()

    def _clock_loop(self):
        while not self._stop.is_set():
            try:
                self.set('uptime', system.secs_to_hhmmss(system.uptime()))
            except Exception as e:
                logging.debug('clock tick: %s', e)
            self._stop.wait(1.0)

    def _render_loop(self):
        delay = 1.0 / max(0.5, self.settings.tune('fps'))
        failures = 0
        while not self._stop.is_set():
            self._dirty.wait(delay)
            if self._stop.is_set():
                break
            self._dirty.clear()
            try:
                self._check_dim()
                self.render()
                failures = 0
            except Exception as e:
                failures += 1
                if failures <= 3 or failures % 200 == 0:
                    logging.warning('render failed (%d in a row): %s',
                                    failures, e)
                self._stop.wait(min(MAX_RENDER_BACKOFF, delay * failures))
                self.invalidate()

    def hold_render(self):
        with self._lock:
            self._render_held += 1

    def release_render(self):
        with self._lock:
            if self._render_held > 0:
                self._render_held -= 1

    def render(self, force=False):
        with self._paint:
            if self._render_held and not force:
                return
            if self._frozen and not force:
                return
            self._render(force)

    def _render(self, force=False):
        menu = self._menu
        if menu is not None:
            menu.render()
            return
        th = look.get(self.settings.get('theme'))
        lay = self.layout
        with self._lock:
            full = force or self._repaint_all
            regions = set(lay.regions) if full else set(self._regions)
            self._repaint_all = False
            self._regions = set()
        if not regions:
            return
        with self.display.frame() as d:
            if full:
                d.clear(th['bg'])
                d.fill_rect(0, lay.header_y, lay.width, lay.header_h, th['panel'])
                d.fill_rect(0, lay.footer_y, lay.width, lay.footer_h, th['panel'])
                d.hline(0, lay.header_h, lay.width, th['edge'])
                d.hline(0, lay.footer_y - 1, lay.width, th['edge'])
                d.vline(lay.split_x, lay.body_top + 6,
                        lay.body_bottom - lay.body_top - 12, th['line'])
            else:
                for key in regions:
                    x, y, w, h = lay.regions[key]
                    d.fill_rect(x, y, w, h, th[lay.bands.get(key, 'bg')])
            for key in regions:
                self._draw_field(d, th, key)
