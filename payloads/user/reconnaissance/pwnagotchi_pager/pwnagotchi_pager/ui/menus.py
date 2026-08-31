import time

from .. import config, interfaces, pool as pool_mod
from .. import stats as stats_mod, system
from . import look
from .look import (LARGE_SIZE, MEDIUM_SIZE, MICRO_SIZE, MONO, SMALL_SIZE, UI,
                     UI_BOLD, wrap)

ROW_H = 21
SEP_H = 18
HEAD_H = 28
FOOT_H = 20
TOP = HEAD_H + 5
SIDE = 16
MARK_W = 3
TRACK_W = 4
FOOTER_GAP = FOOT_H + 6


class Item:
    selectable = True
    heading = False

    def __init__(self, label, value=None, action=None, adjust=None, hint=None,
                 tone=None, enabled=True, repaints_all=False):
        self.label = label
        self._value = value
        self.action = action
        self.adjust = adjust
        self.hint = hint
        self.tone = tone
        self.enabled = enabled
        self.repaints_all = repaints_all

    def value(self):
        if callable(self._value):
            return self._value()
        return self._value

    @property
    def label(self):
        return self._label() if callable(self._label) else self._label

    @label.setter
    def label(self, value):
        self._label = value

    @property
    def enabled(self):
        return self._enabled() if callable(self._enabled) else self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value

    def colour(self, th):
        if not self.enabled:
            return th['dim']
        if self.tone == 'bool':
            return th['on'] if self.value() in ('ON', 'Yes') else th['off']
        if self.tone == 'accent':
            return th['accent']
        return th['text']


class Separator(Item):
    selectable = False
    heading = True

    def __init__(self, label=''):
        Item.__init__(self, label)


class Reading(Item):
    selectable = False
    heading = False

    def __init__(self, label, value=None, tone=None):
        Item.__init__(self, label, value=value, tone=tone)


PLACEHOLDER = Separator('')


class Menu:
    def __init__(self, screen, title, items, footer=None, on_back=None,
                 title_font=None):
        self.screen = screen
        self.display = screen.display
        self.settings = screen.settings
        self.title = title
        self.items = items
        self.footer = footer
        self.on_back = on_back
        self.index = self._first_selectable(0, 1)
        self.result = None
        self.done = False
        self._scroll = 0
        self._pending = None
        self._footer_text = None
        self._position_text = None
        self._position_w = 0
        self._painted = None

    def forget(self):
        self._painted = None
        self._footer_text = None
        self._position_text = None

    def _first_selectable(self, start, step):
        n = len(self.items)
        if not n:
            return 0
        for offset in range(n):
            idx = (start + offset * step) % n
            if self.items[idx].selectable and self.items[idx].enabled:
                return idx
        return 0

    def move(self, step):
        previous = self.index
        scrolled_from = self._scroll
        self.index = self._first_selectable(self.index + step, step)
        start, end = self.visible_rows()
        if (start == scrolled_from and start <= previous < end
                and start <= self.index < end):
            self._pending = {previous, self.index}
        else:
            self._pending = None
        self.screen.wake()

    @property
    def current(self):
        if not self.items:
            return PLACEHOLDER
        return self.items[min(self.index, len(self.items) - 1)]

    def handle(self, button):
        if not self.items and button != 'back':
            return None
        if button == 'up':
            self.move(-1)
        elif button == 'down':
            self.move(1)
        elif button in ('left', 'right'):
            item = self.current
            if item.adjust and item.enabled:
                item.adjust(1 if button == 'right' else -1)
                self._pending = None if item.repaints_all else {self.index}
                self.screen.wake()
        elif button == 'select':
            item = self.current
            if not item.enabled:
                return self.result
            if item.action:
                self.result = item.action()
                if self.result is not None:
                    self.done = True
            elif item.adjust:
                item.adjust(1)
                self._pending = None if item.repaints_all else {self.index}
                self.screen.wake()
        elif button == 'back':
            if self.on_back:
                self.result = self.on_back()
                self.done = self.result is not None
            else:
                self.done = True
        return self.result

    def _row_h(self, item):
        return SEP_H if item.heading else ROW_H

    def _fits_from(self, start, available):
        used = 0
        end = start
        while end < len(self.items):
            h = self._row_h(self.items[end])
            if used + h > available:
                break
            used += h
            end += 1
        return max(end, start + 1)

    def visible_rows(self):
        if not self.items:
            return 0, 0
        available = self.display.height - TOP - FOOTER_GAP
        start = max(0, min(self._scroll, len(self.items) - 1))
        end = self._fits_from(start, available)
        while self.index >= end and start < len(self.items) - 1:
            start += 1
            end = self._fits_from(start, available)
        while self.index < start and start > 0:
            start -= 1
            end = self._fits_from(start, available)
        self._scroll = start
        return start, end

    def _painted_state(self, theme_name, start, end):
        rows = tuple(
            (idx, self.items[idx].label, self.items[idx].enabled,
             None if self.items[idx].heading else self.items[idx].value(),
             idx == self.index)
            for idx in range(start, end))
        return (theme_name, start, end, self._footer_value(), rows)

    def _covers_changes(self, state, pending):
        painted = self._painted
        if painted is None:
            return False
        if state[:3] != painted[:3]:
            return False
        previous = {row[0]: row for row in painted[4]}
        for row in state[4]:
            if row[0] in pending:
                continue
            if previous.get(row[0]) != row:
                return False
        return True

    def render(self):
        theme_name = self.settings.get('theme')
        th = look.get(theme_name)
        w = self.display.width
        start, end = self.visible_rows()
        pending, self._pending = self._pending, None
        state = self._painted_state(theme_name, start, end)
        if pending is None and state == self._painted:
            return
        if pending is not None and not self._covers_changes(state, pending):
            pending = None
        self._painted = state
        if pending is not None:
            self._render_rows(th, w, start, end, pending)
            return
        with self.display.frame() as d:
            d.clear(th['bg'])
            self._draw_header(d, th, w)
            for idx, (y, _h) in self._row_geometry(start, end).items():
                self._draw_row(d, th, w, idx, y)
            self._draw_scrollbar(d, th, w, start, end)
            self._draw_footer(d, th, w)

    def _draw_header(self, d, th, w):
        d.fill_rect(0, 0, w, HEAD_H, th['panel'])
        d.hline(0, HEAD_H, w, th['edge'])
        height = d.ttf_height(UI_BOLD, MEDIUM_SIZE) or int(MEDIUM_SIZE * 1.3)
        d.draw_ttf(SIDE, (HEAD_H - height) // 2, self.title, th['title'],
                   UI_BOLD, MEDIUM_SIZE)
        self._draw_position(d, th, w, repaint=True)

    def _draw_position(self, d, th, w, repaint=False):
        selectable = [i for i in self.items if i.selectable and i.enabled]
        if len(selectable) <= 1:
            return
        try:
            position = selectable.index(self.current) + 1
        except ValueError:
            position = 1
        text = '%d/%d' % (position, len(selectable))
        if text == self._position_text and not repaint:
            return
        self._position_text = text
        small = d.ttf_height(MONO, MICRO_SIZE) or int(MICRO_SIZE * 1.3)
        width = d.ttf_width(text, MONO, MICRO_SIZE) or 0
        box = max(width, self._position_w) + 12
        self._position_w = width
        if repaint:
            d.fill_rect(w - SIDE - box, 1, box, HEAD_H - 1, th['panel'])
        d.draw_ttf(w - SIDE - width, (HEAD_H - small) // 2, text,
                   th['dim'], MONO, MICRO_SIZE)

    def _draw_scrollbar(self, d, th, w, start, end):
        total = len(self.items)
        if total <= 0 or (start == 0 and end >= total):
            return
        top = TOP
        height = self.display.height - FOOTER_GAP - top
        if height <= 8:
            return
        x = w - TRACK_W - 3
        d.fill_rect(x, top, TRACK_W, height, th['line'])
        span = max(1, end - start)
        thumb = max(12, int(height * span / float(total)))
        offset = int(height * start / float(total))
        offset = min(offset, height - thumb)
        d.fill_rect(x, top + offset, TRACK_W, thumb, th['accent'])


    def _row_geometry(self, start, end):
        y = TOP
        out = {}
        for idx in range(start, end):
            h = self._row_h(self.items[idx])
            out[idx] = (y, h)
            y += h
        return out

    def _render_rows(self, th, w, start, end, rows):
        geo = self._row_geometry(start, end)
        with self.display.frame() as d:
            for idx in rows:
                if idx not in geo:
                    continue
                y, h = geo[idx]
                d.fill_rect(0, y, w, h, th['bg'])
                self._draw_row(d, th, w, idx, y)
            self._draw_scrollbar(d, th, w, start, end)
            self._draw_position(d, th, w, repaint=True)
            self._draw_footer(d, th, w, only_if_changed=True)

    def _draw_row(self, d, th, w, idx, y):
        item = self.items[idx]
        selected = item.selectable and idx == self.index
        if item.heading:
            if item.label:
                height = d.ttf_height(UI_BOLD, MICRO_SIZE) or int(MICRO_SIZE * 1.3)
                top = y + SEP_H - height - 4
                d.draw_ttf(SIDE, top, item.label, th['dim'], UI_BOLD, MICRO_SIZE)
                width = d.ttf_width(item.label, UI_BOLD, MICRO_SIZE) or 0
                rule = SIDE + width + 8
                d.hline(rule, top + height // 2, max(0, w - rule - SIDE - 6),
                        th['line'])
            return
        height = d.ttf_height(UI, MEDIUM_SIZE) or int(MEDIUM_SIZE * 1.3)
        top = y + (ROW_H - height) // 2
        if selected:
            d.fill_rect(0, y, w - 10, ROW_H, th['chip'])
            d.fill_rect(0, y, MARK_W, ROW_H, th['accent'])
        if not item.enabled:
            label_colour = th['dim']
        elif selected:
            label_colour = th['text']
        elif not item.selectable:
            label_colour = th['label']
        else:
            label_colour = th['unselected']
        value = item.value()
        text = None if value is None else str(value)
        taken = 0
        if text is not None:
            taken = (d.ttf_width(text, UI_BOLD, SMALL_SIZE) or 0) + 6
        budget = w - 2 * SIDE - taken
        label = item.label
        while label and (d.ttf_width(label, UI, MEDIUM_SIZE) or 0) > budget:
            label = label[:-1]
        d.draw_ttf(SIDE, top, label, label_colour, UI, MEDIUM_SIZE)
        if text is not None:
            vheight = d.ttf_height(UI_BOLD, SMALL_SIZE) or int(SMALL_SIZE * 1.3)
            d.draw_ttf(w - SIDE - taken, y + (ROW_H - vheight) // 2, text,
                       item.colour(th), UI_BOLD, SMALL_SIZE)

    def _footer_value(self):
        hint = self.current.hint if self.current.selectable else None
        if callable(hint):
            hint = hint()
        footer = self.footer() if callable(self.footer) else self.footer
        return hint or footer

    def _draw_footer(self, d, th, w, only_if_changed=False):
        footer = self._footer_value()
        if only_if_changed and footer == self._footer_text:
            return
        self._footer_text = footer
        y = self.display.height - FOOT_H
        d.fill_rect(0, y, w, FOOT_H, th['panel'])
        d.hline(0, y, w, th['edge'])
        if footer:
            budget = w - 2 * SIDE
            while footer and (d.ttf_width(footer, UI, SMALL_SIZE) or 0) > budget:
                footer = footer[:-1]
            height = d.ttf_height(UI, SMALL_SIZE) or int(SMALL_SIZE * 1.3)
            d.draw_ttf(SIDE, y + (FOOT_H - height) // 2, footer, th['dim'],
                       UI, SMALL_SIZE)


class MenuRunner:
    def __init__(self, screen, inputs):
        self.screen = screen
        self.inputs = inputs

    def run(self, menu):
        self.screen.attach_menu(menu)
        self.inputs.drain()
        try:
            self.screen.invalidate()
            while not menu.done:
                button = self.inputs.wait(timeout=0.2)
                if button is None:
                    if not self.screen.display.alive:
                        break
                    continue
                self.screen.touch()
                menu.handle(button)
            return menu.result
        finally:
            self.screen.detach_menu()

    def confirm(self, title, subtitle=''):
        choice = {'value': False}

        def yes():
            choice['value'] = True
            return 'done'

        items = [
            Separator(subtitle) if subtitle else Separator(),
            Item('Yes', action=yes),
            Item('No', action=lambda: 'done'),
        ]
        self.run(Menu(self.screen, title, items))
        return choice['value']

    def notice(self, title, lines, footer='Press any button'):
        th = look.get(self.screen.settings.get('theme'))
        d = self.screen.display
        self.screen.hold_render()
        try:
            self._draw_notice(th, d, title, lines, footer)
            self.inputs.drain()
            self.inputs.wait(timeout=120)
        finally:
            self.screen.release_render()
        self.screen.invalidate()

    def _draw_notice(self, th, d, title, lines, footer):
        with d.frame() as f:
            f.clear(th['bg'])
            f.fill_rect(0, 0, d.width, HEAD_H, th['panel'])
            f.hline(0, HEAD_H, d.width, th['edge'])
            height = d.ttf_height(UI_BOLD, MEDIUM_SIZE) or int(MEDIUM_SIZE * 1.3)
            f.draw_ttf(SIDE, (HEAD_H - height) // 2, title, th['warning'],
                       UI_BOLD, MEDIUM_SIZE)
            y = HEAD_H + 22
            for line in lines:
                for part in wrap(d, line, d.width - 2 * SIDE, UI, SMALL_SIZE):
                    f.draw_ttf(SIDE, y, part, th['text'], UI, SMALL_SIZE)
                    y += 20
            f.fill_rect(0, d.height - FOOT_H, d.width, FOOT_H, th['panel'])
            f.hline(0, d.height - FOOT_H, d.width, th['edge'])
            small = d.ttf_height(UI, SMALL_SIZE) or int(SMALL_SIZE * 1.3)
            f.draw_ttf(SIDE, d.height - FOOT_H + (FOOT_H - small) // 2, footer,
                       th['dim'], UI, SMALL_SIZE)


def _cycle(settings, key, options):
    def adjust(step):
        settings.cycle(key, options, step)
    return adjust


def _toggle(settings, key):
    def adjust(step=1):
        settings.toggle(key)
    return adjust


def _yes_no(settings, key):
    return lambda: 'Yes' if settings.get(key) else 'No'


def _on_off(settings, key):
    return lambda: 'ON' if settings.get(key) else 'OFF'


def iface_label(settings):
    def label():
        choice = settings.get('iface_choice')
        status = interfaces.mk7ac_status()
        if choice == interfaces.AUTO:
            return 'Auto (MK7AC)' if status == interfaces.READY else 'Auto (built-in)'
        if choice == interfaces.MK7AC_FIRST and status != interfaces.READY:
            return 'MK7AC (%s)' % ('absent' if status == interfaces.ABSENT
                                   else 'not monitor')
        return {interfaces.BUILTIN_ONLY: 'Built-in',
                interfaces.MK7AC_FIRST: 'MK7AC'}.get(choice, 'Auto')
    return label


_RADIO_CACHE = {'key': None, 'at': 0.0, 'text': ''}
RADIO_CACHE_TTL = 5.0


def radio_footer(settings):
    key = (settings.get('iface_choice'), bool(settings.get('single_pmkid')))
    now = time.monotonic()
    if _RADIO_CACHE['key'] == key and now - _RADIO_CACHE['at'] < RADIO_CACHE_TTL:
        return _RADIO_CACHE['text']
    plan = interfaces.resolve(key[0], avoid=pool_mod.reserved_phys(settings))
    text = plan.describe()
    if not (plan.dedicated_pmkid or key[1]):
        text = text.replace('PMKID sweep', 'PMKID off')
    _RADIO_CACHE.update(key=key, at=now, text=text)
    return text


def startup_menu(screen, runner, settings, editors):
    result = {}

    def start():
        result['action'] = 'start'
        return 'start'

    def quit_app():
        if runner.confirm('Exit Pwnagotchi Pager?'):
            result['action'] = 'exit'
            return 'exit'
        return None

    def dedicated():
        return (interfaces.mk7ac_status() == interfaces.READY
                and settings.get('iface_choice') in (interfaces.AUTO,
                                                     interfaces.MK7AC_FIRST))

    def pmkid_value():
        if dedicated():
            return 'n/a'
        return 'Yes' if settings.get('single_pmkid') else 'No'

    def pmkid_adjust(step=1):
        if not dedicated():
            settings.toggle('single_pmkid')

    def pmkid_hint():
        return ('MK7AC hunts PMKID constantly' if dedicated()
                else 'Borrow the capture radio now and then')

    items = [
        Item('Start hunting', action=start, hint='GREEN to begin'),
        Separator('RADIO'),
        Item('Interface', value=iface_label(settings), tone='accent',
             adjust=_cycle(settings, 'iface_choice', list(interfaces.CHOICES)),
             hint='LEFT/RIGHT to change radio'),
        Item('PMKID sweeps', value=pmkid_value, tone='bool',
             adjust=pmkid_adjust, enabled=lambda: not dedicated(),
             hint=pmkid_hint),
        Separator('TARGETING'),
        Item('Skip captured', value=_yes_no(settings, 'skip_captured'), tone='bool',
             adjust=_toggle(settings, 'skip_captured'),
             hint='Stop attacking once captured'),
        Item('Cover sibling APs', value=_yes_no(settings, 'cover_siblings'), tone='bool',
             adjust=_toggle(settings, 'cover_siblings'),
             hint="Treat one router's radios as one"),
        Item('SSID pool', value=_on_off(settings, 'pool_enabled'), tone='bool',
             adjust=_toggle(settings, 'pool_enabled'),
             hint='Advertise networks clients ask for'),
        Item('Deauth scope', value='>', action=editors['scope'],
             hint='Who to protect, who to hit'),
        Separator('OUTPUT'),
        Item('Logging', value='>', action=editors['logging'],
             hint='Access point log and debug'),
        Item('Feedback', value='>', action=editors['feedback'],
             hint='LEDs, vibration and sound'),
        Separator(),
        Item('Exit', action=quit_app),
    ]
    menu = Menu(screen, 'pwnagotchi pager', items,
                footer=lambda: radio_footer(settings), on_back=quit_app)
    runner.run(menu)
    return result.get('action', 'exit')


def logging_menu(screen, runner, settings):
    items = [
        Item('Log access points', value=_yes_no(settings, 'log_aps_enabled'),
             tone='bool', adjust=_toggle(settings, 'log_aps_enabled'),
             hint='JSON log of every AP seen'),
        Separator(),
        Item('Back', action=lambda: 'back'),
    ]
    runner.run(Menu(screen, 'LOGGING', items))
    return None


def feedback_menu(screen, runner, settings):
    items = [
        Item('D-pad LEDs', value=_on_off(settings, 'leds_enabled'), tone='bool',
             adjust=_toggle(settings, 'leds_enabled'), hint='Colour shows what it is doing'),
        Item('Vibration', value=_on_off(settings, 'haptics_enabled'), tone='bool',
             adjust=_toggle(settings, 'haptics_enabled'), hint='Buzz on a new handshake'),
        Item('Sound', value=_on_off(settings, 'sound_enabled'), tone='bool',
             adjust=_toggle(settings, 'sound_enabled'), hint='Jingle on a new handshake'),
        Separator(),
        Item('Back', action=lambda: 'back'),
    ]
    runner.run(Menu(screen, 'FEEDBACK', items))
    return None


def scope_menu(screen, runner, settings, list_editor):
    def counts(key):
        return lambda: str(len(settings.get(key)))

    items = [
        Item('Deauth', value=_on_off(settings, 'deauth_enabled'), tone='bool',
             adjust=_toggle(settings, 'deauth_enabled'),
             hint='Master switch for deauth frames'),
        Separator('LISTS'),
        Item('Never attack', value=counts('whitelist'), tone='accent',
             action=lambda: list_editor('whitelist'),
             hint='Protected - this list always wins'),
        Item('Attack only these', value=counts('blacklist'), tone='accent',
             action=lambda: list_editor('blacklist'),
             hint='Leave empty to attack everything else'),
        Separator(),
        Item('Back', action=lambda: 'back'),
    ]
    runner.run(Menu(screen, 'DEAUTH SCOPE', items))
    return None


def _rate(value):
    if value >= 10:
        return '%.0f/h' % value
    if value >= 0.1:
        return '%.1f/h' % value
    return '--' if not value else '%.2f/h' % value


def yield_menu(screen, runner, agent):
    y = agent.stats

    def session():
        return '%d in %s' % (y.total, system.secs_to_hhmmss(y.runtime()))

    def split():
        return '%d / %d / %d' % (y.captures[stats_mod.DEAUTH],
                                 y.captures[stats_mod.PMKID],
                                 y.captures[stats_mod.PINEAP])

    def lifetime():
        return '%d' % (sum(y.lifetime[k] for k in stats_mod.KINDS) + y.total)

    def cost():
        spent = y.cost()
        return '--' if not spent else system.secs_to_hhmmss(spent)

    items = [
        Separator('THIS RUN'),
        Reading('Captured', session, tone='accent'),
        Reading('Rate', lambda: _rate(y.per_hour())),
        Reading('Deauth / PMKID / PineAP', split),
        Reading('Attempts', lambda: str(y.attempts)),
        Reading('Airtime per capture', cost),
        Separator('ALL RUNS'),
        Reading('Captured', lifetime, tone='accent'),
        Reading('Rate', lambda: _rate(y.per_hour(lifetime=True))),
    ]
    ranked = y.ranked(5)
    if ranked:
        items.append(Separator('BEST CHANNELS'))
        for row in ranked:
            items.append(Reading(
                'ch %s' % row['channel'],
                '%d in %s  %s' % (row['captures'],
                                  system.secs_to_hhmmss(row['airtime']),
                                  _rate(row['rate']))))
    barren = y.barren()
    if barren:
        items.append(Separator('NOTHING YET'))
        items.append(Reading('Channels', ', '.join(str(c) for c in barren[:8])))
    items.append(Separator())
    items.append(Item('Back', action=lambda: 'back'))
    runner.run(Menu(screen, 'YIELD', items))


def pause_menu(screen, runner, settings, agent):
    outcome = {}

    def choose(name):
        def action():
            outcome['value'] = name
            return name
        return action

    def resume():
        outcome['value'] = 'resume'
        return 'resume'

    items = [
        Item('Resume', action=resume, hint='RED also resumes'),
        Separator('DISPLAY'),
        Item('Theme', value=lambda: settings.get('theme'), tone='accent',
             adjust=_cycle(settings, 'theme', config.THEME_NAMES),
             repaints_all=True),
        Item('Brightness', value=lambda: '%d%%' % settings.get('brightness'),
             tone='accent', adjust=_cycle(settings, 'brightness', config.BRIGHTNESS_STEPS)),
        Item('Auto dim', value=lambda: ('Off' if not settings.get('auto_dim')
                                        else '%ds' % settings.get('auto_dim')),
             tone='accent', adjust=_cycle(settings, 'auto_dim', config.AUTO_DIM_OPTIONS)),
        Item('Dim level', value=lambda: '%d%%' % settings.get('auto_dim_level'),
             tone='accent', adjust=_cycle(settings, 'auto_dim_level', config.AUTO_DIM_LEVELS)),
        Separator('BEHAVIOUR'),
        Item('Deauth', value=_on_off(settings, 'deauth_enabled'), tone='bool',
             adjust=_toggle(settings, 'deauth_enabled')),
        Item('Skip captured', value=_yes_no(settings, 'skip_captured'), tone='bool',
             adjust=_toggle(settings, 'skip_captured')),
        Item('SSID pool', value=_on_off(settings, 'pool_enabled'), tone='bool',
             adjust=_toggle(settings, 'pool_enabled'),
             hint='Advertise networks clients ask for'),
        Item('D-pad LEDs', value=_on_off(settings, 'leds_enabled'), tone='bool',
             adjust=_toggle(settings, 'leds_enabled')),
        Separator(),
        Item('Yield', action=lambda: yield_menu(screen, runner, agent),
             hint='What the hunting is actually returning',
             enabled=agent is not None),
        Item('Main menu', action=choose('menu')),
        Item('Exit', action=choose('exit')),
    ]
    footer = (lambda: agent.status_line()) if agent else None
    runner.run(Menu(screen, 'PAUSED', items, footer=footer, on_back=resume))
    return outcome.get('value', 'resume')


CHARSET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_. :'


def text_entry(screen, runner, title, subtitle=''):
    screen.hold_render()
    try:
        return _text_entry(screen, runner, title, subtitle)
    finally:
        screen.release_render()
        screen.invalidate()


def _text_entry(screen, runner, title, subtitle=''):
    display = screen.display
    inputs = runner.inputs
    text = ''
    index = 0

    def draw():
        t = look.get(screen.settings.get('theme'))
        with display.frame() as d:
            d.clear(t['bg'])
            d.fill_rect(0, 0, display.width, 34, t['panel'])
            d.draw_ttf_centered(4, title, t['title'], UI_BOLD, MEDIUM_SIZE)
            if subtitle:
                d.draw_ttf_centered(40, subtitle, t['dim'], UI, SMALL_SIZE)
            shown = text or '_'
            d.draw_ttf_centered(66, shown[-26:], t['text'], MONO, MEDIUM_SIZE)
            d.hline(SIDE, 96, display.width - 2 * SIDE, t['line'])
            span = 9
            start = max(0, min(index - span // 2, len(CHARSET) - span))
            x = (display.width - span * 30) // 2
            for i in range(start, min(len(CHARSET), start + span)):
                ch = CHARSET[i]
                if i == index:
                    d.fill_rect(x - 4, 110, 28, 30, t['panel'])
                    d.draw_ttf(x, 114, ch, t['selected'], MONO, LARGE_SIZE)
                else:
                    d.draw_ttf(x, 114, ch, t['unselected'], MONO, MEDIUM_SIZE)
                x += 30
            d.hline(0, display.height - 20, display.width, t['line'])
            d.draw_ttf_centered(display.height - 16,
                                'GREEN add  UP delete  DOWN save  RED cancel',
                                t['dim'], UI, SMALL_SIZE)

    inputs.drain()
    draw()
    while True:
        button = inputs.wait(timeout=0.25)
        if button is None:
            if not display.alive:
                return None
            continue
        screen.touch()
        if button == 'left':
            index = (index - 1) % len(CHARSET)
        elif button == 'right':
            index = (index + 1) % len(CHARSET)
        elif button == 'select':
            if len(text) < 32:
                text += CHARSET[index]
        elif button == 'up':
            text = text[:-1]
        elif button == 'down':
            return text.strip()
        elif button == 'back':
            return None
        draw()
