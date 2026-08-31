#!/bin/bash
# Title: Pwnagotchi Pager
# Description: Automated WiFi handshake and PMKID capture with personality
# Version: 2.0
# Category: Reconnaissance

PAYLOAD_DIR="/root/payloads/user/reconnaissance/pwnagotchi_pager"
DATA_DIR="$PAYLOAD_DIR/data"
LOOT_DIR="/root/loot/PwnagotchiPager"

cd "$PAYLOAD_DIR" || {
    LOG "red" "ERROR: $PAYLOAD_DIR not found"
    exit 1
}

export PATH="/mmc/usr/bin:$PAYLOAD_DIR/bin:$PATH"
export PYTHONPATH="$PAYLOAD_DIR/lib:$PAYLOAD_DIR:$PYTHONPATH"
export LD_LIBRARY_PATH="/mmc/usr/lib:$PAYLOAD_DIR/lib:$LD_LIBRARY_PATH"

if [ ! -f "$PAYLOAD_DIR/lib/libpagerctl.so" ] || [ ! -f "$PAYLOAD_DIR/lib/pagerctl.py" ]; then
    for dir in /mmc/root/payloads/user/utilities/PAGERCTL; do
        if [ -f "$dir/libpagerctl.so" ] && [ -f "$dir/pagerctl.py" ]; then
            mkdir -p "$PAYLOAD_DIR/lib"
            cp "$dir/libpagerctl.so" "$dir/pagerctl.py" "$PAYLOAD_DIR/lib/"
            LOG "green" "Copied pagerctl from $dir"
            break
        fi
    done
fi

if [ ! -f "$PAYLOAD_DIR/lib/libpagerctl.so" ] || [ ! -f "$PAYLOAD_DIR/lib/pagerctl.py" ]; then
    LOG "red" "=== MISSING DEPENDENCY ==="
    LOG "red" "libpagerctl.so or pagerctl.py not found"
    LOG "Install the PAGERCTL payload or copy it to $PAYLOAD_DIR/lib/"
    LOG "Press any button to exit..."
    WAIT_FOR_INPUT >/dev/null 2>&1
    exit 1
fi

prompt_install() {
    LOG ""
    LOG "green" "GREEN = Install now (needs internet)"
    LOG "red" "RED   = Exit"
    LOG ""
    while true; do
        BUTTON=$(WAIT_FOR_INPUT 2>/dev/null)
        [ -z "$BUTTON" ] && sleep 0.1
        case "$BUTTON" in
            "GREEN"|"A") return 0 ;;
            "RED"|"B") return 1 ;;
        esac
    done
}

opkg_install() {
    LOG "Updating package lists..."
    opkg update 2>&1 | while IFS= read -r line; do LOG "  $line"; done
    LOG "Installing $*..."
    opkg -d mmc install "$@" 2>&1 | while IFS= read -r line; do LOG "  $line"; done
}

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "import ctypes" 2>/dev/null; then
    LOG "red" "=== MISSING REQUIREMENT ==="
    LOG "Python3 with ctypes is required."
    if prompt_install; then
        opkg_install python3 python3-ctypes
        if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "import ctypes" 2>/dev/null; then
            LOG "red" "Install failed. Check your internet connection."
            LOG "Press any button to exit..."
            WAIT_FOR_INPUT >/dev/null 2>&1
            exit 1
        fi
        LOG "green" "Python3 installed."
    else
        exit 0
    fi
fi

MISSING=""
for tool in hcxdumptool hcxpcapngtool; do
    command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"
done

if [ -n "$MISSING" ]; then
    LOG "red" "=== MISSING REQUIREMENT ==="
    LOG "red" "Required tool(s) not found:$MISSING"
    LOG ""
    LOG "Pwnagotchi Pager cannot capture without these."
    LOG ""
    LOG "yellow" "Install them, then relaunch:"
    LOG "  opkg update"
    LOG "  opkg install hcxdumptool hcxtools"
    LOG ""
    LOG "Or place the binaries in:"
    LOG "  $PAYLOAD_DIR/bin/"
    LOG ""
    LOG "Press any button to exit..."
    WAIT_FOR_INPUT >/dev/null 2>&1
    exit 1
fi

[ ! -d "$PAYLOAD_DIR/pwnagotchi_pager" ] && {
    LOG "red" "ERROR: pwnagotchi_pager module not found"
    exit 1
}

RUNNING=$(ps w 2>/dev/null | grep "[r]un_pwnagotchi_pager" | awk '{print $1}' | head -1)
if [ -n "$RUNNING" ]; then
    LOG "red" "=== ALREADY RUNNING ==="
    LOG "Pwnagotchi Pager is already running as PID $RUNNING."
    LOG ""
    LOG "Starting a second copy would fight it for the radios and stop"
    LOG "its capture, so this one is backing out."
    LOG ""
    LOG "Press any button to exit..."
    WAIT_FOR_INPUT >/dev/null 2>&1
    exit 1
fi

HANDSHAKE_DIR="$LOOT_DIR/handshakes"
if [ -f "$PAYLOAD_DIR/config.conf" ]; then
    CONFIGURED=$(sed -n 's/^[[:space:]]*handshakes_dir[[:space:]]*=[[:space:]]*//p' "$PAYLOAD_DIR/config.conf" | tail -1 | sed 's/[[:space:]]*$//')
    [ -n "$CONFIGURED" ] && HANDSHAKE_DIR="$CONFIGURED"
fi

mkdir -p "$DATA_DIR" "$HANDSHAKE_DIR" 2>/dev/null
if [ ! -d "$HANDSHAKE_DIR" ] || [ ! -w "$HANDSHAKE_DIR" ]; then
    HANDSHAKE_DIR="$LOOT_DIR/handshakes"
    mkdir -p "$HANDSHAKE_DIR" 2>/dev/null
fi

pineapd_pid() {
    for p in /proc/[0-9]*; do
        [ -r "$p/cmdline" ] || continue
        case "$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)" in
            /usr/sbin/pineapd\ *) echo "${p#/proc/}"; return 0 ;;
        esac
    done
    return 1
}

pineapd_args() {
    pid=$(pineapd_pid) || return 1
    running=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null)
    [ -z "$running" ] && return 1
    printf '%s\n' "$running" | awk '
        NR == 1 { next }
        $0 == "--interface" || $0 == "--band" || $0 == "--type" ||
        $0 == "--hop" || $0 == "--primary" || $0 == "--inject" { skip = 2; next }
        $0 == "--handshakepath" || $0 == "--wiglepath" { skip = 2; next }
        $0 == "--handshakes" || $0 == "--partialhandshakes" { skip = 2; next }
        $0 ~ /^--(interface|band|type|hop|primary|inject)=/ { next }
        $0 ~ /^--(handshakepath|wiglepath|wigle)=/ { next }
        $0 ~ /^--(handshakes|partialhandshakes)=/ { next }
        skip > 1 { skip = 0; next }
        { print }
    '
    return 0
}

bring_radios_up() {
    for i in wlan0mon wlan1mon wlan2mon; do
        [ -e "/sys/class/net/$i" ] || continue
        ip link set "$i" up 2>/dev/null
    done
}

iface_bands() {
    _cfg=$(uci -q get "pineapd.$1.bands" 2>/dev/null)
    if [ -n "$_cfg" ]; then
        echo "$_cfg"
        return 0
    fi
    _phy=$(basename "$(readlink -f "/sys/class/net/$1/phy80211" 2>/dev/null)" 2>/dev/null)
    if [ -z "$_phy" ] || [ ! -d "/sys/class/ieee80211/$_phy" ]; then
        echo "2,5"
        return 0
    fi
    iw phy "$_phy" info 2>/dev/null | awk '
        /^[ 	]*Band [0-9]+:/ {
            n = $2; sub(":", "", n)
            b = ""
            if (n == 1) b = "2"
            else if (n == 2) b = "5"
            else if (n == 4) b = "6"
            next
        }
        b != "" && /MHz \[/ && $0 !~ /disabled/ { seen[b] = 1 }
        END {
            out = ""
            split("2 5 6", order, " ")
            for (i = 1; i <= 3; i++)
                if (seen[order[i]]) out = (out == "" ? order[i] : out "," order[i])
            print (out == "" ? "2,5" : out)
        }'
}

pineapd_radios() {
    for i in wlan1mon wlan0mon wlan2mon; do
        [ -e "/sys/class/net/$i" ] || continue
        bands=$(iface_bands "$i")
        printf -- '--interface\n%s\n--band\n%s:%s\n--type\n%s:max\n--hop\n%s:fast\n' \
            "$i" "$i" "$bands" "$i" "$i"
        if [ -z "$PRIMARY" ]; then
            PRIMARY="$i"
            printf -- '--primary\n%s\n--inject\n%s\n' "$i" "$i"
        fi
    done
}

restore_macs() {
    for i in wlan0mon wlan1mon wlan2mon; do
        [ -e "/sys/class/net/$i" ] || continue
        perm=$(ethtool -P "$i" 2>/dev/null | awk '{print $NF}')
        cur=$(cat "/sys/class/net/$i/address" 2>/dev/null)
        if [ -n "$perm" ] && [ "$perm" != "$cur" ] && [ "$perm" != "00:00:00:00:00:00" ]; then
            ip link set "$i" down 2>/dev/null
            ip link set "$i" address "$perm" 2>/dev/null
            ip link set "$i" up 2>/dev/null
        fi
    done
}

_restored=0
restore_services() {
    [ "$_restored" = "1" ] && return
    _restored=1
    if [ -n "$APP_PID" ]; then
        kill -TERM "$APP_PID" 2>/dev/null
        i=0
        while [ $i -lt 90 ] && kill -0 "$APP_PID" 2>/dev/null; do
            sleep 1
            i=$((i + 1))
        done
        kill -KILL "$APP_PID" 2>/dev/null
    fi
    rm -f "$PAYLOAD_DIR/data/pineapd.argv" 2>/dev/null
    [ -n "$PINEAPD_PID" ] && kill "$PINEAPD_PID" 2>/dev/null
    killall hcxdumptool 2>/dev/null
    i=0
    while [ $i -lt 5 ] && pidof hcxdumptool >/dev/null 2>&1; do
        sleep 1
        i=$((i + 1))
    done
    killall -9 hcxdumptool 2>/dev/null
    restore_macs
    killall pineapd 2>/dev/null
    sleep 1
    /etc/init.d/pineapd start 2>/dev/null
    /etc/init.d/php8-fpm start 2>/dev/null
    /etc/init.d/nginx start 2>/dev/null
    /etc/init.d/pineapplepager start 2>/dev/null
}
LOG ""
LOG "green" "Pwnagotchi Pager"
LOG "cyan" "Automated WiFi handshake and PMKID capture"
LOG ""
LOG "yellow" "Features:"
LOG "cyan" "  - Handshake and PMKID capture"
LOG "cyan" "  - Uses the MK7AC as a second radio when present"
LOG "cyan" "  - Whitelist / blacklist targeting"
LOG "cyan" "  - Stops attacking networks it already captured"
LOG "cyan" "  - Adapts as you move and as radios come and go"
LOG ""
LOG "green" "GREEN = Start"
LOG "red" "RED = Exit"
LOG ""

while true; do
    BUTTON=$(WAIT_FOR_INPUT 2>/dev/null)
    [ -z "$BUTTON" ] && sleep 0.1
    case "$BUTTON" in
        "GREEN"|"A") break ;;
        "RED"|"B") LOG "Exiting."; exit 0 ;;
    esac
done

LOG ""
SPINNER_ID=$(START_SPINNER "Setting up...")

RADIOS=""
for i in wlan0mon wlan1mon wlan2mon; do
    [ -e "/sys/class/net/$i" ] && RADIOS="$RADIOS $i"
done

STOP_SPINNER "$SPINNER_ID" 2>/dev/null

if [ -z "$RADIOS" ]; then
    LOG "red" "No monitor interfaces found - nothing to capture with."
    LOG "Press any button to exit..."
    WAIT_FOR_INPUT >/dev/null 2>&1
    exit 1
fi
LOG "green" "Radios:$RADIOS"
if ! echo "$RADIOS" | grep -q wlan1mon; then
    LOG "yellow" "wlan1mon not found - capture may be limited"
fi

KEEP_ARGS=$(pineapd_args)
if [ -n "$KEEP_ARGS" ]; then
    LOG "cyan" "Keeping the running pineapd options"
else
    LOG "yellow" "pineapd was not running - using defaults"
    KEEP_ARGS=$(printf -- '--recon=true\n--reconpath\n/root/recon/\n--reconname\npager\n--handshakes=true\n--partialhandshakes=true\n')
fi
LOG "cyan" "Handshakes: $HANDSHAKE_DIR"
LOG "Starting, the pager display takes over from here..."

trap 'restore_services; exit' INT TERM HUP
trap restore_services EXIT

/etc/init.d/php8-fpm stop 2>/dev/null
/etc/init.d/nginx stop 2>/dev/null
/etc/init.d/pineapplepager stop 2>/dev/null

/etc/init.d/pineapd stop 2>/dev/null
killall pineapd 2>/dev/null
sleep 1
bring_radios_up

PRIMARY=""
PINEAPD_ARGV=$(
    {
        printf '%s\n' "$KEEP_ARGS"
        printf -- '--handshakepath\n%s/\n' "$HANDSHAKE_DIR"
        printf -- '--handshakes=true\n--partialhandshakes=true\n'
        printf -- '--wigle=false\n'
        pineapd_radios
    } | grep -v '^[[:space:]]*$'
)

set -f
OLD_IFS="$IFS"
IFS='
'
set -- $PINEAPD_ARGV
IFS="$OLD_IFS"
set +f

mkdir -p "$PAYLOAD_DIR/data" 2>/dev/null
printf '%s
' "$PINEAPD_ARGV" > "$PAYLOAD_DIR/data/pineapd.argv" 2>/dev/null
( ulimit -d 131072 2>/dev/null; exec /usr/sbin/pineapd "$@" ) &
PINEAPD_PID=$!
sleep 2

if ! kill -0 "$PINEAPD_PID" 2>/dev/null; then
    echo "Warning: pineapd may not have started" >&2
fi

cd "$PAYLOAD_DIR" || exit 1
python3 run_pwnagotchi_pager.py &
APP_PID=$!
wait "$APP_PID"
EXIT_CODE=$?
APP_PID=""

killall hcxdumptool 2>/dev/null
i=0
while [ $i -lt 5 ] && pidof hcxdumptool >/dev/null 2>&1; do
    sleep 1
    i=$((i + 1))
done
killall -9 hcxdumptool 2>/dev/null
restore_macs
exit $EXIT_CODE
