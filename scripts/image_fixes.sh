#!/bin/bash
# ===========================================================================
# image_fixes.sh — Fixes applied to a PiKaraoke rootfs so the Pi boots
# straight into karaoke with no prompts. Sourced by build_pikaraoke_image.sh
# and patch_pikaraoke_image.sh.
#
#   apply_pikaraoke_fixes <ROOTFS>
#
# ROOTFS must be mounted with boot at $ROOTFS/boot/firmware and /dev, /proc,
# /sys bind-mounted (chroot + network needed for pip).
# ===========================================================================

PK_USER="${TARGET_USER:-pikaraoke}"
PK_FIXES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PK_COUNTRY="BR"
PK_TIMEZONE="America/Sao_Paulo"
PK_LOCALE="pt_BR.UTF-8"
PK_KEYBOARD="br"

_fx_log() { echo -e "\033[0;32m[FIX]\033[0m $*"; }

apply_pikaraoke_fixes() {
    local R="$1"
    local BOOT="$R/boot/firmware"
    [ -d "$BOOT" ] || BOOT="$R/boot"
    [ -f "$R/etc/os-release" ] || { echo "apply_pikaraoke_fixes: $R is not a rootfs" >&2; return 1; }

    # ── 1. Python packages ────────────────────────────────────────────────
    # pygame comes from apt (python3-pygame 2.1.2); pip pygame==2.1.0 does not
    # build on Python 3.11 and used to abort the whole pip install.
    _fx_log "Installing Python packages"
    chroot "$R" /bin/bash -ec "
        dpkg -s python3-pygame >/dev/null 2>&1 || {
            apt-get update
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-pygame
            apt-get clean; rm -rf /var/lib/apt/lists/*
        }
        pip3 install --break-system-packages --retries 10 --timeout 60 \
            'tempora==1.14.0' 'jaraco.functools==2.0' 'CherryPy==10.2.1' \
            'qrcode[pil]' unidecode 'flask-paginate==2021.10.29' \
            'Babel==2.9.1' 'Flask-Babel==2.0.0'
    "

    # CherryPy 10.2.1 uses inspect.getargspec, removed in Python 3.11
    local CPD
    CPD=$(ls "$R"/usr/local/lib/python3*/dist-packages/cherrypy/_cpdispatch.py 2>/dev/null | head -1)
    if [ -n "$CPD" ]; then
        sed -i 's/\bgetargspec\b/getfullargspec/g' "$CPD"
        _fx_log "Patched CherryPy for Python 3.11"
    fi

    chroot "$R" /bin/bash -ec "cd /home/$PK_USER/pikaraoke && python3 -c 'import cherrypy, flask, flask_babel, flask_paginate, qrcode, unidecode, pygame, psutil'"
    _fx_log "Python imports OK"

    # ── 2. systemd ordering ───────────────────────────────────────────────
    # pikaraoke After=graphical.target + pikaraoke-ws After=pikaraoke, both
    # WantedBy=graphical.target -> ordering cycle; systemd dropped pikaraoke-ws.
    local SVC="$R/etc/systemd/system/pikaraoke.service"
    if [ -f "$SVC" ]; then
        sed -i 's/^After=.*/After=network-online.target sound.target display-manager.service\nWants=network-online.target/' "$SVC"
        sed -i '/^Wants=mnt-pikaraoke/d' "$SVC"
        sed -i 's/^StartLimitBurst=.*/StartLimitBurst=20/' "$SVC"
        # keep only one Wants=network-online.target
        awk '!(/^Wants=network-online.target$/ && seen++)' "$SVC" > "$SVC.tmp" && mv "$SVC.tmp" "$SVC"
        _fx_log "Fixed pikaraoke.service ordering"
    fi

    # ── 3. SSH on by default ──────────────────────────────────────────────
    chroot "$R" systemctl enable ssh.service
    rm -f "$R/etc/ssh/sshd_config.d/rename_user.conf"
    _fx_log "SSH enabled"

    # ── 4. No first-boot wizard / user creation ───────────────────────────
    rm -f "$R/etc/xdg/autostart/piwiz.desktop"
    chroot "$R" /bin/bash -c "
        systemctl disable userconfig.service 2>/dev/null || true
        systemctl mask userconfig.service 2>/dev/null || true
        if id rpi-first-boot-wizard >/dev/null 2>&1; then userdel -r rpi-first-boot-wizard 2>/dev/null || userdel rpi-first-boot-wizard; fi
        rm -f /etc/sudoers.d/*wizard* 2>/dev/null || true
    "
    rm -f "$BOOT/userconf" "$BOOT/userconf.txt"
    _fx_log "First-boot wizard removed (user: $PK_USER / password: pikaraoke)"

    # ── 5. Locale, time zone, keyboard ────────────────────────────────────
    chroot "$R" /bin/bash -ec "
        sed -i 's/^# *\($PK_LOCALE UTF-8\)/\1/' /etc/locale.gen
        locale-gen
        update-locale LANG=$PK_LOCALE LANGUAGE=$PK_LOCALE
        ln -sf /usr/share/zoneinfo/$PK_TIMEZONE /etc/localtime
        echo '$PK_TIMEZONE' > /etc/timezone
    "
    [ -f "$R/etc/default/keyboard" ] && sed -i "s/^XKBLAYOUT=.*/XKBLAYOUT=\"$PK_KEYBOARD\"/" "$R/etc/default/keyboard"
    _fx_log "Locale $PK_LOCALE, time zone $PK_TIMEZONE, keyboard $PK_KEYBOARD"

    # ── 6. Wi-Fi country (Wi-Fi is rfkill-blocked until it is set) ────────
    if [ -f "$BOOT/cmdline.txt" ]; then
        sed -i 's/ *cfg80211.ieee80211_regdom=[A-Z]*//' "$BOOT/cmdline.txt"
        sed -i "1 s/\$/ cfg80211.ieee80211_regdom=$PK_COUNTRY/" "$BOOT/cmdline.txt"
    fi
    mkdir -p "$R/var/lib/systemd/rfkill"
    for dev in platform-3f300000.mmcnr:wlan platform-fe300000.mmcnr:wlan platform-1001100000.mmc:wlan; do
        echo 0 > "$R/var/lib/systemd/rfkill/$dev"
    done
    for f in "$R"/var/lib/systemd/rfkill/*:wlan; do echo 0 > "$f"; done
    # Raspberry Pi OS ships NetworkManager with Wi-Fi off until raspi-config
    # sets the country; without this wlan0 stays "unavailable".
    mkdir -p "$R/var/lib/NetworkManager"
    local NMS="$R/var/lib/NetworkManager/NetworkManager.state"
    if [ -f "$NMS" ] && grep -q '^WirelessEnabled=' "$NMS"; then
        sed -i 's/^WirelessEnabled=.*/WirelessEnabled=true/' "$NMS"
    else
        printf '[main]\nWirelessEnabled=true\n' >> "$NMS"
    fi
    _fx_log "Wi-Fi country $PK_COUNTRY, Wi-Fi unblocked"

    # ── 7. pikaraoke-wifi.txt: set country first, wait for NetworkManager ─
    if [ -f "$R/home/$PK_USER/setup-wifi.sh" ]; then
        cat > "$R/home/$PK_USER/setup-wifi.sh" << 'WIFISETUP'
#!/bin/bash
# Reads /boot/firmware/pikaraoke-wifi.txt (SSID=, PASSWORD=, COUNTRY=) and
# creates a NetworkManager connection for it.
WIFI_FILE=/boot/firmware/pikaraoke-wifi.txt
[ -f "$WIFI_FILE" ] || exit 0
val() { grep "^$1=" "$WIFI_FILE" | head -1 | cut -d= -f2- | tr -d '\r'; }
SSID=$(val SSID); PASSWORD=$(val PASSWORD); COUNTRY=$(val COUNTRY)
[ -n "$SSID" ] || exit 0
if [ -n "$COUNTRY" ]; then
    raspi-config nonint do_wifi_country "$COUNTRY" 2>/dev/null || iw reg set "$COUNTRY" 2>/dev/null || true
fi
rfkill unblock wifi 2>/dev/null || true
for i in $(seq 1 30); do nmcli -t general status >/dev/null 2>&1 && break; sleep 2; done
nmcli connection delete pikaraoke-wifi >/dev/null 2>&1 || true
if [ -n "$PASSWORD" ]; then
    nmcli connection add type wifi ifname wlan0 con-name pikaraoke-wifi ssid "$SSID" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD" connection.autoconnect yes
else
    nmcli connection add type wifi ifname wlan0 con-name pikaraoke-wifi ssid "$SSID" connection.autoconnect yes
fi
nmcli connection up pikaraoke-wifi || true
rm -f "$WIFI_FILE"
echo "WiFi configured: $SSID"
WIFISETUP
        chmod +x "$R/home/$PK_USER/setup-wifi.sh"
        chown 1000:1000 "$R/home/$PK_USER/setup-wifi.sh"
        # Run on every boot (not only first boot) so the file can be added later
        cat > "$R/etc/systemd/system/pikaraoke-wifi-setup.service" << EOF
[Unit]
Description=PiKaraoke Wi-Fi from boot partition
After=NetworkManager.service
Wants=NetworkManager.service
ConditionPathExists=/boot/firmware/pikaraoke-wifi.txt

[Service]
Type=oneshot
ExecStart=/bin/bash /home/$PK_USER/setup-wifi.sh

[Install]
WantedBy=multi-user.target
EOF
        sed -i '/^ExecStartPre=.*setup-wifi.sh/d' "$R/etc/systemd/system/pikaraoke-first-boot.service" 2>/dev/null || true
        chroot "$R" systemctl enable pikaraoke-wifi-setup.service
        _fx_log "Wi-Fi file setup fixed"
    fi

    # ── 8. Wi-Fi setup hotspot (myviKaraoke-Config + captive page) ────────
    if [ -f "$PK_FIXES_DIR/pikaraoke-wifi-portal.py" ]; then
        install -m 755 "$PK_FIXES_DIR/pikaraoke-wifi-portal.py" "$R/usr/local/bin/pikaraoke-wifi-portal"
        mkdir -p "$R/etc/NetworkManager/dnsmasq-shared.d"
        echo "address=/#/10.42.0.1" > "$R/etc/NetworkManager/dnsmasq-shared.d/pikaraoke-captive.conf"
        cat > "$R/etc/systemd/system/pikaraoke-wifi-portal.service" << 'EOF'
[Unit]
Description=PiKaraoke Wi-Fi setup hotspot
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/pikaraoke-wifi-portal
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
        chroot "$R" systemctl enable pikaraoke-wifi-portal.service
        _fx_log "Wi-Fi setup hotspot installed (SSID myviKaraoke-Config)"
    else
        echo "WARNING: pikaraoke-wifi-portal.py not found next to image_fixes.sh" >&2
    fi

    # ── 11. Source-based routing (cable + Wi-Fi in the same network) ──────
    if [ -f "$PK_FIXES_DIR/pikaraoke-source-routing" ]; then
        mkdir -p "$R/etc/NetworkManager/dispatcher.d"
        install -m 755 -o root -g root "$PK_FIXES_DIR/pikaraoke-source-routing" \
            "$R/etc/NetworkManager/dispatcher.d/50-pikaraoke-source-routing"
        _fx_log "Source-based routing installed (Wi-Fi IP answers with cable plugged in)"
    fi

    # ── 10. Song library: app code + empty database ───────────────────────
    # The app scans the song folder on startup and imports what it finds;
    # the image must not carry a database listing songs it does not have.
    local REPO_DIR APP_DIR SONGS_DIR
    REPO_DIR="$(dirname "$PK_FIXES_DIR")"
    APP_DIR="$R/home/$PK_USER/pikaraoke"
    SONGS_DIR="$R/home/$PK_USER/pikaraoke-songs"
    if [ -d "$APP_DIR" ] && [ -f "$REPO_DIR/lib/song_library.py" ]; then
        mkdir -p "$APP_DIR/lib" "$APP_DIR/db"
        install -m 644 "$REPO_DIR/karaoke.py"             "$APP_DIR/karaoke.py"
        install -m 644 "$REPO_DIR/lib/song_library.py"    "$APP_DIR/lib/song_library.py"
        install -m 644 "$REPO_DIR/db/schema.sql"          "$APP_DIR/db/schema.sql"
        chown -R 1000:1000 "$APP_DIR"
        _fx_log "App updated with song folder scan"
    fi
    local WS_DIR="$R/home/$PK_USER/pikaraoke-ws"
    if [ -d "$WS_DIR" ] && [ -f "$REPO_DIR/pikaraoke-ws/server.py" ]; then
        cp -r "$REPO_DIR/pikaraoke-ws/." "$WS_DIR/"
        find "$WS_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
        chown -R 1000:1000 "$WS_DIR"
        _fx_log "pikaraoke-ws updated from the repo"
    fi
    for d in "$SONGS_DIR" "$R/mnt/pikaraoke-songs"; do
        if [ -f "$d/karaoke.db" ]; then
            rm -f "$d/karaoke.db" "$d/karaoke.db-journal" "$d/karaoke.db-wal" "$d/karaoke.db-shm"
            _fx_log "Removed pre-filled $d/karaoke.db (rebuilt from the folder on first start)"
        fi
    done

    # ── 9. Admin SSH keys (scripts/authorized_keys, one key per line) ─────
    if [ -s "$PK_FIXES_DIR/authorized_keys" ]; then
        mkdir -p "$R/home/$PK_USER/.ssh"
        cp "$PK_FIXES_DIR/authorized_keys" "$R/home/$PK_USER/.ssh/authorized_keys"
        chmod 700 "$R/home/$PK_USER/.ssh"; chmod 600 "$R/home/$PK_USER/.ssh/authorized_keys"
        chown -R 1000:1000 "$R/home/$PK_USER/.ssh"
        _fx_log "SSH keys installed for $PK_USER"
    fi
}
