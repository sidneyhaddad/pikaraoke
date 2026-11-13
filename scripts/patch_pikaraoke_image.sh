#!/bin/bash
# ===========================================================================
# patch_pikaraoke_image.sh — Apply image_fixes.sh to an existing image
# without rebuilding it from scratch.
#
# Usage:  sudo ./patch_pikaraoke_image.sh <input.img.xz> <output.img.xz>
# ===========================================================================
set -euo pipefail

IN="$1"; OUT="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=image_fixes.sh
source "$SCRIPT_DIR/image_fixes.sh"

[ "$(id -u)" -eq 0 ] || { echo "Must run as root" >&2; exit 1; }
[ -f "$IN" ] || { echo "Missing $IN" >&2; exit 1; }

WORK="$(dirname "$(readlink -f "$OUT")")"
IMG="$WORK/$(basename "$OUT" .xz)"
ROOTFS="$WORK/rootfs-patch"
LOOP=""

cleanup() {
    for m in tmp sys proc dev/pts dev boot/firmware ""; do
        mountpoint -q "$ROOTFS/$m" 2>/dev/null && umount "$ROOTFS/$m" || true
    done
    [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null || true
}
trap cleanup EXIT

echo "[PATCH] Decompressing $IN -> $IMG"
xz -dc -T0 "$IN" > "$IMG"

LOOP=$(losetup --find --show --partscan "$IMG")
sleep 2
mkdir -p "$ROOTFS"
mount "${LOOP}p2" "$ROOTFS"
mount "${LOOP}p1" "$ROOTFS/boot/firmware"
mount --bind /dev     "$ROOTFS/dev"
mount --bind /dev/pts "$ROOTFS/dev/pts"
mount -t proc proc    "$ROOTFS/proc"
mount -t sysfs sysfs  "$ROOTFS/sys"
mount -t tmpfs tmpfs  "$ROOTFS/tmp"

cp "$ROOTFS/etc/resolv.conf" "$WORK/resolv.conf.bak" 2>/dev/null || true
cp --remove-destination /etc/resolv.conf "$ROOTFS/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$ROOTFS/usr/sbin/policy-rc.d"; chmod +x "$ROOTFS/usr/sbin/policy-rc.d"

apply_pikaraoke_fixes "$ROOTFS"

rm -f "$ROOTFS/usr/sbin/policy-rc.d"
[ -f "$WORK/resolv.conf.bak" ] && cp --remove-destination "$WORK/resolv.conf.bak" "$ROOTFS/etc/resolv.conf" && rm -f "$WORK/resolv.conf.bak"
find "$ROOTFS/home" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
rm -rf "$ROOTFS/root/.cache/pip"

cleanup
LOOP=""
LOOP=$(losetup --find --show --partscan "$IMG"); sleep 1
e2fsck -f -y "${LOOP}p2" >/dev/null || true
losetup -d "$LOOP"; LOOP=""

echo "[PATCH] Compressing -> $OUT"
rm -f "$OUT"
xz -6 -T0 --memlimit-compress=70% -v "$IMG"
[ "$IMG.xz" = "$OUT" ] || mv "$IMG.xz" "$OUT"
sha256sum "$OUT"
echo "[PATCH] Done"
