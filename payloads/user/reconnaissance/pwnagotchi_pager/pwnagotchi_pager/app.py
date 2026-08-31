import logging
import signal
import sys
import threading
import time

from . import config, interfaces, pool as pool_mod, system
from .system import Feedback
from .agent import Agent
from .ui import lists, menus
from .ui.pager import Display, DisplayError, Input
from .ui.screen import Screen

EPOCHS_BEFORE_COMPLAINING = 3

REQUIREMENTS = [
    ('hcxdumptool', 'PMKID and handshake capture'),
    ('hcxpcapngtool', 'converting captures to .22000'),
    ('_pineap', 'radio control'),
    ('iw', 'radio detection'),
]

OPTIONAL = [
    ('ethtool', 'reading the permanent MAC so it can be restored after '
                'hcxdumptool randomises it'),
]

INSTALL_HINT = [
    'Install it, then relaunch:',
    'opkg update && opkg install hcxdumptool',
    'or drop the binary in the payload bin/ folder',
]


def missing_requirements():
    return [(tool, why) for tool, why in REQUIREMENTS if not system.have(tool)]


def warn_optional():
    for tool, why in OPTIONAL:
        if not system.have(tool):
            logging.warning('%s is not installed - %s', tool, why)


def _report_missing(missing, display=None, settings=None):
    names = ', '.join(tool for tool, _ in missing)
    sys.stderr.write('\nPwnagotchi Pager cannot start.\n')
    sys.stderr.write('Missing required tool(s): %s\n\n' % names)
    for tool, why in missing:
        sys.stderr.write('  %-16s needed for %s\n' % (tool, why))
    sys.stderr.write('\n')
    for line in INSTALL_HINT:
        sys.stderr.write('  %s\n' % line)
    sys.stderr.write('\n')
    if display is None or settings is None:
        return
    try:
        from .ui import look
        from .ui.look import FONT, LARGE_SIZE, MEDIUM_SIZE, SMALL_SIZE
        th = look.get(settings.get('theme'))
        with display.frame() as d:
            d.clear(th['bg'])
            d.draw_ttf_centered(14, 'MISSING DEPENDENCY', th['off'], FONT, LARGE_SIZE)
            d.draw_ttf_centered(52, names, th['text'], FONT, MEDIUM_SIZE)
            y = 84
            for line in INSTALL_HINT:
                d.draw_ttf_centered(y, line, th['dim'], FONT, SMALL_SIZE)
                y += 20
            d.draw_ttf_centered(display.height - 26, 'Any button to exit',
                                th['warning'], FONT, SMALL_SIZE)
        Input(display).wait(timeout=120)
    except Exception as e:
        logging.debug('could not draw dependency screen: %s', e)


class Session:
    def __init__(self, agent, screen, inputs, runner, settings):
        self.agent = agent
        self.screen = screen
        self.inputs = inputs
        self.runner = runner
        self.settings = settings
        self.outcome = 'exit'
        self._buttons = None
        self._stop = threading.Event()

    def _button_loop(self):
        while not self._stop.is_set():
            try:
                if self._handle_button() is False:
                    break
            except Exception as e:
                logging.exception('button handling failed: %s', e)
                if self._stop.wait(1.0):
                    break

    def _handle_button(self):
        button = self.inputs.wait(timeout=0.2)
        if button is None:
            return None if self.screen.display.alive else False
        logging.debug('button %s', button)
        if self.screen.touch():
            return None
        if button != 'back':
            return None
        logging.info('paused')
        self.agent.pause()
        choice = 'resume'
        try:
            choice = menus.pause_menu(self.screen, self.runner,
                                      self.settings, self.agent)
        finally:
            if choice not in ('exit', 'menu'):
                self.agent.resume()
        if choice == 'exit':
            logging.info('exit requested')
            self.agent.request_exit()
            self.outcome = 'exit'
            self.screen.freeze('Shutting down...', 'putting the radios back')
            return False
        if choice == 'menu':
            logging.info('returning to the startup menu')
            self.agent.request_menu()
            self.outcome = 'menu'
            self.screen.freeze('Returning to menu...', 'please wait')
            return False
        return None

    def run(self):
        self._buttons = threading.Thread(target=self._button_loop,
                                         name='buttons', daemon=True)
        try:
            self.agent.start()
            self.inputs.drain()
            self._buttons.start()
            failures = 0
            while not self.agent.should_stop():
                try:
                    self.agent.run_epoch()
                    failures = 0
                except Exception as e:
                    failures += 1
                    logging.exception('epoch failed (%d in a row): %s',
                                      failures, e)
                    if failures == EPOCHS_BEFORE_COMPLAINING:
                        self.agent.report_trouble()
                    if failures >= EPOCHS_BEFORE_COMPLAINING:
                        self.screen.set_status('epochs keep failing - '
                                               'check the log')
                    if not self.agent.sleep(min(60, 5 * failures)):
                        break
        finally:
            self._stop.set()
            try:
                self.agent.stop()
            except Exception as e:
                logging.exception('agent shutdown failed: %s', e)
            if self._buttons.is_alive():
                self._buttons.join(timeout=1.0)
        return self.outcome


def build_menus(screen, runner, settings):
    editor = lists.ListEditor(screen, runner, settings)
    return {
        'scope': lambda: menus.scope_menu(screen, runner, settings, editor.open),
        'logging': lambda: menus.logging_menu(screen, runner, settings),
        'feedback': lambda: menus.feedback_menu(screen, runner, settings),
    }


def run():
    settings = config.Settings()
    config.setup_logging(settings)

    display = Display()
    try:
        display.open()
    except DisplayError as e:
        sys.stderr.write('%s\n' % e)
        return 1

    missing = missing_requirements()
    warn_optional()
    screen = None
    try:
        if missing:
            _report_missing(missing, display, settings)
            return 2

        screen = Screen(display, settings)
        screen.start()
        inputs = Input(display)
        runner = menus.MenuRunner(screen, inputs)
        editors = build_menus(screen, runner, settings)

        stopping = {'value': False}

        def on_signal(sig, frame):
            if stopping['value']:
                logging.info('signal %d again, already shutting down', sig)
                return
            logging.info('signal %d, shutting down', sig)
            stopping['value'] = True
            raise KeyboardInterrupt

        for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, on_signal)

        while not stopping['value']:
            action = menus.startup_menu(screen, runner, settings, editors)
            if action != 'start':
                break

            plan = interfaces.resolve(
                settings.get('iface_choice'), refresh=True,
                avoid=pool_mod.reserved_phys(settings))
            logging.info('radios: %s', plan.describe())
            feedback = Feedback(display, settings)
            try:
                agent = Agent(settings, screen, plan, feedback)
            except Exception as e:
                logging.exception('could not start: %s', e)
                try:
                    feedback.shutdown()
                except Exception:
                    pass
                screen.freeze('Cannot start', str(e)[:44])
                Input(display).wait(timeout=30)
                break
            screen.set('hint', plan.describe())
            session = Session(agent, screen, inputs, runner, settings)
            try:
                outcome = session.run()
            except KeyboardInterrupt:
                agent.request_exit()
                agent.stop()
                outcome = 'exit'
            except Exception as e:
                logging.exception('session failed: %s', e)
                screen.freeze('Something went wrong', str(e)[:40])
                Input(display).wait(timeout=20)
                outcome = 'exit'
            screen.unfreeze()
            if outcome != 'menu':
                break
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if screen is not None:
            try:
                screen.on_shutdown()
                time.sleep(0.4)
            except Exception:
                pass
            screen.stop()
        display.close()


def main():
    return run()
