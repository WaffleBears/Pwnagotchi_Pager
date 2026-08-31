import os
import sys

PAYLOAD_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, PAYLOAD_DIR)
sys.path.insert(0, os.path.join(PAYLOAD_DIR, 'lib'))
os.chdir(PAYLOAD_DIR)

try:
    from pwnagotchi_pager.app import main
except OSError as e:
    sys.stderr.write('Pwnagotchi Pager cannot start: %s\n' % e)
    sys.stderr.write('Install the PAGERCTL payload or copy libpagerctl.so '
                     'into lib/.\n')
    sys.exit(1)

if __name__ == '__main__':
    sys.exit(main())
