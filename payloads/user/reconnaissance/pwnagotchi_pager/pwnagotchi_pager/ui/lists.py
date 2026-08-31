import json
import re
import time

from .. import pineap, scope, system
from .menus import Item, Menu, Separator, text_entry

IN_RANGE = 120

TITLES = {'whitelist': 'NEVER ATTACK', 'blacklist': 'ATTACK ONLY THESE'}
BLURB = {'whitelist': 'Protected - always wins over the other list',
         'blacklist': 'When this list is not empty, nothing else is attacked'}


_MAC_TEXT = re.compile(r'^([0-9A-F]{2}[:-]){5}[0-9A-F]{2}$'
                       r'|^([0-9A-F]{4}\.){2}[0-9A-F]{4}$')
_MAC_SHAPE = re.compile(r'^(\w{2}[:-]){5}\w{2}$|^(\w{4}\.){2}\w{4}$')


def as_bssid(value):
    text = (value or '').strip().upper()
    if not _MAC_TEXT.match(text):
        return ''
    raw = text.replace(':', '').replace('-', '').replace('.', '')
    return ':'.join(raw[i:i + 2] for i in range(0, 12, 2))


def looks_like_bssid(value):
    return bool(_MAC_SHAPE.match((value or '').strip().upper()))


def scan():
    rc, out, _ = system.run_cmd(['_pineap', 'RECON', 'APS', 'format=json',
                                 'limit=120'], timeout=20)
    found = {}
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    now = time.time()
    for raw in pineap.entries(data, 'aps'):
        ap = pineap.parse_ap(raw, now)
        if ap is None or now - ap['last_seen'] > IN_RANGE:
            continue
        key = ap['hostname'] or ap['mac']
        current = found.get(key)
        if current is None or ap['rssi'] > current['rssi']:
            found[key] = {'ssid': ap['hostname'], 'bssid': ap['mac'],
                          'rssi': ap['rssi']}
    return sorted(found.values(), key=lambda e: -e['rssi'])


class ListEditor:
    def __init__(self, screen, runner, settings):
        self.screen = screen
        self.runner = runner
        self.settings = settings

    def entries(self, key):
        return self.settings.get(key)

    def save(self, key, entries):
        self.settings.set(key, entries)

    def add(self, key, ssid, bssid):
        ssid = (ssid or '').strip()
        bssid = as_bssid(bssid)
        if not ssid and not bssid:
            return False
        entries = self.entries(key)
        if scope.listed(entries, ssid, bssid, cover_siblings=False):
            return False
        entries.append({'ssid': ssid, 'bssid': bssid})
        self.save(key, entries)
        return True

    def open(self, key):
        items = [
            Item('Scan and add', value='>', action=lambda: self.scan_add(key),
                 hint='Pick from what is in range'),
            Item('Type an entry', value='>', action=lambda: self.manual(key),
                 hint='Enter an SSID or BSSID by hand'),
            Item('View and remove', value=lambda: str(len(self.entries(key))),
                 tone='accent', action=lambda: self.view(key)),
            Separator(),
            Item('Back', action=lambda: 'back'),
        ]
        self.runner.run(Menu(self.screen, TITLES[key], items, footer=BLURB[key]))
        return None

    def scan_add(self, key):
        self.screen.hold_render()
        try:
            self.screen.freeze('Scanning...', 'reading nearby beacons',
                               sticky=False)
            networks = scan()
        finally:
            self.screen.release_render()
        self.screen.unfreeze()
        if not networks:
            self.runner.notice('No networks', ['Nothing in range yet.',
                                               'Let recon run and try again.'])
            return None

        def make(entry):
            def action():
                added = self.add(key, entry['ssid'], entry['bssid'])
                self.runner.notice('Added' if added else 'Already listed',
                                   [entry['ssid'] or entry['bssid']],
                                   footer='Any button to continue')
                return None
            return action

        items = []
        for entry in networks[:40]:
            def mark(e=entry):
                listed = scope.listed(self.entries(key), e['ssid'],
                                      e['bssid'], cover_siblings=False)
                return ('* ' if listed else '') + (e['ssid']
                                                   or e['bssid'])[:22]
            items.append(Item(mark,
                              value='%ddBm' % entry['rssi'],
                              tone='accent',
                              action=make(entry),
                              hint=entry['bssid']))
        items.append(Separator())
        items.append(Item('Back', action=lambda: 'back'))
        self.runner.run(Menu(self.screen, TITLES[key], items,
                             footer='* already listed'))
        return None

    def manual(self, key):
        value = text_entry(self.screen, self.runner, TITLES[key],
                           'SSID, or a BSSID as AA:BB:CC:DD:EE:FF')
        value = (value or '').strip()
        if not value:
            return None
        bssid = as_bssid(value)
        if not bssid and looks_like_bssid(value):
            self.runner.notice('Not a BSSID',
                               [value, 'Expected AA:BB:CC:DD:EE:FF'],
                               footer='Any button to continue')
            return None
        added = self.add(key, '', bssid) if bssid else self.add(key, value, '')
        self.runner.notice('Added' if added else 'Already listed', [value],
                           footer='Any button to continue')
        return None

    def view(self, key):
        while self._view_once(key) == 'removed':
            if not self.entries(key):
                break
        return None

    def _view_once(self, key):
        entries = self.entries(key)
        if not entries:
            self.runner.notice(TITLES[key], ['This list is empty.'])
            return None

        def remover(entry):
            def action():
                label = entry.get('ssid') or entry.get('bssid')
                if not self.runner.confirm('Remove?', label):
                    return None
                current = [e for e in self.entries(key)
                           if not (e.get('ssid') == entry.get('ssid')
                                   and e.get('bssid') == entry.get('bssid'))]
                self.save(key, current)
                return 'removed'
            return action

        items = []
        for entry in entries:
            label = entry.get('ssid') or entry.get('bssid')
            detail = entry.get('bssid') if entry.get('ssid') else 'BSSID'
            items.append(Item(label[:24], value='x', tone='accent',
                              action=remover(entry), hint=detail))
        items.append(Separator())
        items.append(Item('Back', action=lambda: 'back'))
        result = self.runner.run(Menu(self.screen, TITLES[key], items,
                                      footer='GREEN removes an entry'))
        return 'removed' if result == 'removed' else None
