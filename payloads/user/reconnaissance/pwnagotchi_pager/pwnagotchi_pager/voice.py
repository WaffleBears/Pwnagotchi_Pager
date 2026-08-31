import random

STARTING = [
    'Booting. Hide your handshakes.',
    'Hello. I will be your problem today.',
    'Good morning. I have chosen violence.',
    'Up and running. Somebody is about to have a day.',
]

NORMAL = ['', '...']

BORED = [
    'I have been staring at this channel like it owes me money.',
    'Nothing. I checked twice. Then a third time.',
    'This band is a group chat where nobody types.',
]

SAD = [
    'Nobody has transmitted. I am taking it personally.',
    'One more quiet epoch and I may go insane.',
    'This is fine. I am fine. The channel is dead but I am fine.',
    'Starting to think it is me.',
    'I have started to enjoy the silence. That is the worrying part.',
]

ANGRY = [
    'I am not mad. I am just disassociating.',
    'Do not speak to me. Speak to my antenna.',
    'Everyone within 90 meters is on thin ice.',
]

LONELY = [
    'Ghosted. By a router. Again.',
    'Hello? One beacon? Please?',
    'I have started naming the noise.',
    'Just me and my bad intentions out here.',
]

EXCITED = [
    'So many networks, so little time.',
    'This place is all you can eat and I skipped breakfast.',
    'The air is thick with poor decisions.',
    'This many networks and I want them all.',
]

MOTIVATED = [
    'I would like to thank the routers.',
    'Unstoppable. Like a T-Rex with grabbers.',
    'This is my Super Bowl.',
]

DEMOTIVATED = [
    'That achieved nothing. Cool. Great.',
    'I have had better decades.',
    'Swing, miss, fall over.',
    'Do not put that in the report.',
]

SHUTDOWN = [
    'Off the air. We never met.',
    'Goodnight. Change your passwords.',
    'Antennas down. I regret nothing and remember everything.',
    'Powering off before anyone asks questions.',
]

AWAKENING = ['...', 'Awake. Unfortunately.', 'Back. Miss me?']

HANDSHAKES = [
    'That one is mine now.',
    'Thank you for your cooperation.',
]

DEAUTH_CLIENT = [
    'Turning {who} off and on again.',
    'Politely evicting {who}.',
    'Last call, {who}.',
    'Introducing {who} to silence.',
    'Scheduling unplanned downtime for {who}.',
    'Reminding {who} that I exist.',
    '{who} did not need that connection.',
    '{who} is about to blame their ISP.',
    '{who} is now offline and confused.',
    '{who} is having a moment.',
]

DEAUTH_NETWORK = [
    'Everyone off {who}. Right now.',
    'Emptying {who} out.',
    'Telling everyone on {who} to go home.',
    'Shaking {who} until something falls out.',
    'Asking everyone on {who} to reconnect.',
    'Sweeping {who} clean and waiting.',
    'Nobody stays on {who} while I am here.',
]

ASSOC = [
    'Making unsolicited contact with {who}.',
    'Introducing myself to {who}. Aggressively.',
    'Knocking on {who}. Repeatedly.',
    '{who} seems friendly. We will see.',
    'Starting a conversation {who} did not want.',
]

MISS = [
    '{who} got away. This is not over.',
    '{who} has excellent timing and got away.',
]

NAPPING = [
    'Union-mandated break: {secs}s.',
    'Idle {secs}s. Do not judge me.',
    'Resting my eyes for {secs}s.',
    'Zzz ({secs}s).',
]

WAITING = [
    '...',
    'Sweeping for {secs}s.',
]

ALL_CAPTURED = [
    'Nothing left to take. Awkward.',
    'I peaked here. Move me.',
]

CHANNELS_FILTERED = [
    'Everything worth hitting is on a channel you excluded.',
    'I am only allowed to look at the boring channels.',
]

TOO_FAR = [
    'Everything here is too far away to bother with.',
    'All whispers, no shouting. Move closer.',
    'I can hear them. Barely.',
    'Signal so weak it is basically a rumour.',
]

OUT_OF_SCOPE = [
    'You filtered out everything. I hope you are happy.',
    'Surrounded by networks I am not allowed to like.',
    'All of these are on your do-not-touch list.',
]

MOVED_ON = [
    'The old crowd has gone out of range.',
]

NOTHING_HERE = [
    'Is this thing on?',
    'Dead air. Deeply unhelpful.',
    'Not one beacon. Everyone is hiding.',
    'I have never felt so unwanted.',
]

LISTENING = [
    'Any second now. Any second.',
    'Waiting to see if it worked!',
]

RADIO_TROUBLE = [
    'Something is wrong with the antenna.',
]

POOL = [
    'Pretending to be a network they trust.',
]

PMKID = [
    'Going for PMKID.',
    'No clients required. How modern.',
]


def _seconds(value):
    try:
        value = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, value)


def pick(options, **kw):
    return random.choice(options).format(**kw)


def default():
    return 'Standing by. Ominously.'


def on_starting():
    return pick(STARTING)


def on_normal():
    return pick(NORMAL)


def on_bored():
    return pick(BORED)


def on_sad():
    return pick(SAD)


def on_angry():
    return pick(ANGRY)


def on_lonely():
    return pick(LONELY)


def on_excited():
    return pick(EXCITED)


def on_motivated(reward=0):
    return pick(MOTIVATED)


def on_demotivated(reward=0):
    return pick(DEMOTIVATED)


def on_shutdown():
    return pick(SHUTDOWN)


def on_awakening():
    return pick(AWAKENING)


NAMED_HANDSHAKE = [
    'Got {name}.',
    '{name} is mine.',
    'Bagged {name}.',
    '{name} just handed it over.',
    '{name} gave up the handshake.',
    '{name} is in the bag.',
]


def on_handshakes(count, name=''):
    name = (name or '').strip()
    if count > 1:
        if name:
            return '%d new networks, latest %s' % (count, name)
        return '%d new networks. I have no regrets.' % count
    if name:
        return pick(NAMED_HANDSHAKE, name=name)
    return pick(HANDSHAKES)


def on_deauth(who, targeted=False):
    return pick(DEAUTH_CLIENT if targeted else DEAUTH_NETWORK, who=who)


def on_assoc(who):
    return pick(ASSOC, who=who)


def on_miss(who):
    return pick(MISS, who=who)


def napping_phrase():
    return random.choice(NAPPING)


def waiting_phrase():
    return random.choice(WAITING)


def count_down(template, secs):
    return template.format(secs=_seconds(secs))


def on_all_captured():
    return pick(ALL_CAPTURED)


def on_out_of_scope():
    return pick(OUT_OF_SCOPE)


def on_channels_filtered():
    return pick(CHANNELS_FILTERED)


def on_too_far():
    return pick(TOO_FAR)


def on_nothing_here():
    return pick(NOTHING_HERE)


def on_moved_on():
    return pick(MOVED_ON)


def on_listening():
    return pick(LISTENING)


def on_pool():
    return pick(POOL)


def on_pmkid():
    return pick(PMKID)


def on_radio_trouble():
    return pick(RADIO_TROUBLE)
