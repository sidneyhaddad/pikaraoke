#!/bin/bash
# ===========================================================================
# validate_pikaraoke_image.sh — Validate a built PiKaraoke SD card image
#
# Mounts the image read-only and runs ~30 automated checks.
#
# Usage:  sudo ./validate_pikaraoke_image.sh <image.img or image.img.xz>
# ===========================================================================
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Must run as root (sudo)"; exit 1; }
[ $# -ge 1 ]         || { echo "Usage: $0 <image.img[.xz]>"; exit 1; }

IMAGE="$1"
TARGET_USER="pikaraoke"
WORK_DIR=$(mktemp -d)
ROOTFS="$WORK_DIR/rootfs"
LOOP=""

cleanup() {
    umount "$ROOTFS/boot/firmware" 2>/dev/null || true
    umount "$ROOTFS"               2>/dev/null || true
    [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

# Decompress if needed
if [[ "$IMAGE" == *.xz ]]; then
    echo "Decompressing to temp dir..."
    xz -dkc "$IMAGE" > "$WORK_DIR/image.img"
    IMAGE="$WORK_DIR/image.img"
fi

# Mount
LOOP=$(losetup --find --show --partscan "$IMAGE")
sleep 1
mkdir -p "$ROOTFS"
mount -o ro "${LOOP}p2" "$ROOTFS"
mkdir -p "$ROOTFS/boot/firmware"
mount -o ro "${LOOP}p1" "$ROOTFS/boot/firmware" 2>/dev/null || \
    mount -o ro "${LOOP}p1" "$ROOTFS/boot" 2>/dev/null || true

# ── Test framework ────────────────────────────────────────────────────────
PASS=0
FAIL=0

check() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo -e "  \033[0;32m[OK]\033[0m   $desc"
        PASS=$((PASS + 1))
    else
        echo -e "  \033[0;31m[FAIL]\033[0m $desc"
        FAIL=$((FAIL + 1))
    fi
}

check_file() {
    check "File exists: $1" test -f "$ROOTFS$1"
}

check_dir() {
    check "Dir exists: $1" test -d "$ROOTFS$1"
}

check_grep() {
    local desc="$1" file="$2" pattern="$3"
    check "$desc" grep -q "$pattern" "$ROOTFS$file"
}

# ── Run checks ────────────────────────────────────────────────────────────
echo ""
echo "=== PiKaraoke Image Validation ==="
echo ""

echo "── User & Permissions ──"
check "User '$TARGET_USER' exists"       grep -q "^${TARGET_USER}:" "$ROOTFS/etc/passwd"
check "User in audio group"              grep -q "$TARGET_USER" "$ROOTFS/etc/group"
check_file "/etc/sudoers.d/010_pikaraoke"

echo ""
echo "── Hostname & Network ──"
check_grep "Hostname is myvikaraoke"     "/etc/hostname" "myvikaraoke"
check_file "/etc/avahi/services/pikaraoke.service"

echo ""
echo "── Application Code ──"
check_file "/home/$TARGET_USER/pikaraoke/app.py"
check_file "/home/$TARGET_USER/pikaraoke/karaoke.py"
check_file "/home/$TARGET_USER/pikaraoke/constants.py"
check_file "/home/$TARGET_USER/pikaraoke/lib/vlcclient.py"
check_file "/home/$TARGET_USER/pikaraoke/lib/get_platform.py"
check_file "/home/$TARGET_USER/pikaraoke/myvikaraoke.png"
check_dir  "/home/$TARGET_USER/pikaraoke/templates"
check_dir  "/home/$TARGET_USER/pikaraoke/static"
check_file "/home/$TARGET_USER/pikaraoke-ws/server.py"

echo ""
echo "── Translations ──"
check_file "/home/$TARGET_USER/pikaraoke/translations/pt_BR/LC_MESSAGES/messages.mo"
check_file "/home/$TARGET_USER/pikaraoke/translations/zh_Hans_CN/LC_MESSAGES/messages.mo"

echo ""
echo "── System Packages ──"
for pkg in vlc ffmpeg python3 avahi-daemon; do
    check "Package: $pkg" bash -c "
        grep -r '^Package: ${pkg}$' '$ROOTFS/var/lib/dpkg/info/' 2>/dev/null | head -1 || \
        test -f '$ROOTFS/var/lib/dpkg/info/${pkg}.list'
    "
done

echo ""
echo "── Python Packages ──"
for pkg in flask cherrypy pygame qrcode PIL yt_dlp psutil unidecode fastapi uvicorn; do
    # Check in both system and local dist-packages
    check "Python: $pkg" bash -c "
        ls '$ROOTFS'/usr/lib/python3*/dist-packages/${pkg}* 2>/dev/null | head -1 || \
        ls '$ROOTFS'/usr/local/lib/python3*/dist-packages/${pkg}* 2>/dev/null | head -1
    "
done

echo ""
echo "── Systemd Services ──"
check_file "/etc/systemd/system/pikaraoke.service"
check_file "/etc/systemd/system/pikaraoke-ws.service"
check_file "/etc/systemd/system/pikaraoke-first-boot.service"
# Check enabled (symlink in .wants)
check "pikaraoke.service enabled" bash -c "
    test -L '$ROOTFS/etc/systemd/system/graphical.target.wants/pikaraoke.service' || \
    test -L '$ROOTFS/etc/systemd/system/multi-user.target.wants/pikaraoke.service'
"
check "pikaraoke-ws.service enabled" bash -c "
    test -L '$ROOTFS/etc/systemd/system/graphical.target.wants/pikaraoke-ws.service' || \
    test -L '$ROOTFS/etc/systemd/system/multi-user.target.wants/pikaraoke-ws.service'
"

echo ""
echo "── USB Mount ──"
check_dir  "/mnt/pikaraoke-songs"
check "USB automount unit" bash -c "
    ls '$ROOTFS'/etc/systemd/system/mnt-pikaraoke*automount 2>/dev/null | head -1
"

echo ""
echo "── Boot Configuration ──"
# Check in both /boot/firmware and /boot (prefer /boot/firmware)
BCONF=""
if [ -f "$ROOTFS/boot/firmware/config.txt" ]; then
    BCONF="$ROOTFS/boot/firmware/config.txt"
elif [ -f "$ROOTFS/boot/config.txt" ]; then
    BCONF="$ROOTFS/boot/config.txt"
fi
if [ -n "$BCONF" ]; then
    check "gpu_mem=128"    grep -q "gpu_mem=128"      "$BCONF"
    check "Audio enabled"  grep -q "dtparam=audio=on"  "$BCONF"
else
    check "config.txt found" false
fi

echo ""
echo "── LightDM Autologin ──"
if [ -f "$ROOTFS/etc/lightdm/lightdm.conf" ]; then
    check "Autologin user" grep -q "autologin-user=$TARGET_USER" "$ROOTFS/etc/lightdm/lightdm.conf"
else
    check "LightDM config exists" false
fi

echo ""
echo "── VLC Patch ──"
if [ -f "$ROOTFS/usr/bin/vlc" ]; then
    check "VLC geteuid patched" bash -c "! grep -q 'geteuid' '$ROOTFS/usr/bin/vlc'"
else
    check "VLC binary exists" false
fi

echo ""
echo "── Performance ──"
if [ -f "$ROOTFS/etc/dphys-swapfile" ]; then
    check "Swap 512MB" grep -q "CONF_SWAPSIZE=512" "$ROOTFS/etc/dphys-swapfile"
fi

echo ""
echo "── WiFi Config ──"
check "WiFi example on boot" bash -c "
    test -f '$ROOTFS/boot/firmware/pikaraoke-wifi.txt.example' || \
    test -f '$ROOTFS/boot/pikaraoke-wifi.txt.example'
"

echo ""
echo "── yt-dlp ──"
check "yt-dlp accessible" bash -c "
    test -f '$ROOTFS/home/$TARGET_USER/.local/bin/yt-dlp' || \
    test -f '$ROOTFS/usr/local/bin/yt-dlp' || \
    test -f '$ROOTFS/usr/bin/yt-dlp'
"

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo "==========================================="
echo "  Results: $PASS passed, $FAIL failed"
echo "==========================================="
echo ""

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
