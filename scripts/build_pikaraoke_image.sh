#!/bin/bash
# ===========================================================================
# build_pikaraoke_image.sh — Build a ready-to-flash PiKaraoke SD card image
#
# Runs on a Raspberry Pi 4 (aarch64). Downloads Raspberry Pi OS Desktop,
# chroots in, provisions everything, and outputs a compressed .img.xz.
#
# Usage:  sudo ./build_pikaraoke_image.sh
# ===========================================================================
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
IMAGE_URL="https://downloads.raspberrypi.com/raspios_arm64/images/raspios_arm64-2024-11-19/2024-11-19-raspios-bookworm-arm64.img.xz"
IMAGE_BASE="raspios-bookworm-arm64.img"
WORK_DIR="/home/shaddad/Documents/pikaraoke/image-build"
PIKARAOKE_SRC="/home/shaddad/Documents/pikaraoke"
PIKARAOKE_WS_SRC="$PIKARAOKE_SRC/pikaraoke-ws"
TARGET_USER="pikaraoke"
TARGET_HOSTNAME="myvikaraoke"
FINAL_IMAGE="pikaraoke-$(date +%Y%m%d).img"
IMAGE_SIZE_GB=6

# ── Helpers ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[BUILD]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FATAL]${NC} $*" >&2; exit 1; }

LOOP=""
ROOTFS=""
MOUNTS_DONE=false

cleanup() {
    log "Cleaning up..."
    if $MOUNTS_DONE && [ -n "$ROOTFS" ]; then
        umount "$ROOTFS/tmp"           2>/dev/null || true
        umount "$ROOTFS/sys"           2>/dev/null || true
        umount "$ROOTFS/proc"          2>/dev/null || true
        umount "$ROOTFS/dev/pts"       2>/dev/null || true
        umount "$ROOTFS/dev"           2>/dev/null || true
        umount "$ROOTFS/boot/firmware" 2>/dev/null || true
        umount "$ROOTFS"               2>/dev/null || true
    fi
    if [ -n "$LOOP" ]; then
        losetup -d "$LOOP" 2>/dev/null || true
    fi
    # Remove policy-rc.d if left behind
    rm -f "$ROOTFS/usr/sbin/policy-rc.d" 2>/dev/null || true
}
trap cleanup EXIT

run_in_chroot() {
    chroot "$ROOTFS" /bin/bash -ec "$1"
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=image_fixes.sh
source "$SCRIPT_DIR/image_fixes.sh"

# ── Phase 0: Prerequisites ────────────────────────────────────────────────
log "Phase 0: Checking prerequisites"

[ "$(id -u)" -eq 0 ] || die "Must run as root (use sudo)"
[ "$(uname -m)" = "aarch64" ] || die "Must run on aarch64 host"

# Install tools if missing
for tool in losetup parted e2fsck resize2fs rsync wget xz; do
    command -v "$tool" >/dev/null || die "Missing required tool: $tool"
done

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# ── Phase 1: Download and prepare image ───────────────────────────────────
log "Phase 1: Preparing base image"

if [ ! -f "${IMAGE_BASE}.xz" ]; then
    log "Downloading Raspberry Pi OS Desktop arm64..."
    wget -O "${IMAGE_BASE}.xz" "$IMAGE_URL"
fi

if [ ! -f "$IMAGE_BASE" ]; then
    log "Decompressing image..."
    xz -dk "${IMAGE_BASE}.xz"
fi

log "Creating working copy: $FINAL_IMAGE"
cp "$IMAGE_BASE" "$FINAL_IMAGE"

log "Expanding image to ${IMAGE_SIZE_GB}GB..."
truncate -s "${IMAGE_SIZE_GB}G" "$FINAL_IMAGE"

log "Setting up loop device..."
LOOP=$(losetup --find --show --partscan "$FINAL_IMAGE")
log "Loop device: $LOOP"

# Wait for partition devices to appear
sleep 2
[ -b "${LOOP}p1" ] || die "Boot partition ${LOOP}p1 not found"
[ -b "${LOOP}p2" ] || die "Root partition ${LOOP}p2 not found"

log "Expanding root partition..."
parted -s "$LOOP" resizepart 2 100%
e2fsck -f -y "${LOOP}p2" || true
resize2fs "${LOOP}p2"

# ── Phase 2: Mount filesystems ────────────────────────────────────────────
log "Phase 2: Mounting filesystems"

ROOTFS="$WORK_DIR/rootfs"
mkdir -p "$ROOTFS"
mount "${LOOP}p2" "$ROOTFS"
mkdir -p "$ROOTFS/boot/firmware"
mount "${LOOP}p1" "$ROOTFS/boot/firmware"

# Bind-mount system filesystems for chroot
mount --bind /dev     "$ROOTFS/dev"
mount --bind /dev/pts "$ROOTFS/dev/pts"
mount -t proc proc    "$ROOTFS/proc"
mount -t sysfs sysfs  "$ROOTFS/sys"
mount -t tmpfs tmpfs  "$ROOTFS/tmp"
MOUNTS_DONE=true

# DNS for apt inside chroot
cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"

# Prevent services from starting during install
cat > "$ROOTFS/usr/sbin/policy-rc.d" << 'POLICY'
#!/bin/sh
exit 101
POLICY
chmod +x "$ROOTFS/usr/sbin/policy-rc.d"

# ── Phase 3: User and system configuration ────────────────────────────────
log "Phase 3: System configuration"

# Create user
log "Creating user: $TARGET_USER"
run_in_chroot "
    # Remove default pi user if present
    if id pi &>/dev/null; then
        userdel -r pi 2>/dev/null || true
    fi

    # Create pikaraoke user (uid 1000)
    if ! id $TARGET_USER &>/dev/null; then
        useradd -m -u 1000 -s /bin/bash \
            -G sudo,audio,video,input,render,plugdev \
            $TARGET_USER
    fi
    echo '${TARGET_USER}:pikaraoke' | chpasswd

    # Passwordless sudo
    echo '${TARGET_USER} ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/010_pikaraoke
    chmod 0440 /etc/sudoers.d/010_pikaraoke
"

# Hostname
log "Setting hostname: $TARGET_HOSTNAME"
echo "$TARGET_HOSTNAME" > "$ROOTFS/etc/hostname"
sed -i "s/127.0.1.1.*/127.0.1.1\t$TARGET_HOSTNAME/" "$ROOTFS/etc/hosts"
grep -q "127.0.1.1" "$ROOTFS/etc/hosts" || \
    echo "127.0.1.1	$TARGET_HOSTNAME" >> "$ROOTFS/etc/hosts"

# Autologin
log "Configuring autologin"
LIGHTDM_CONF="$ROOTFS/etc/lightdm/lightdm.conf"
if [ -f "$LIGHTDM_CONF" ]; then
    if grep -q "^autologin-user=" "$LIGHTDM_CONF"; then
        sed -i "s/^autologin-user=.*/autologin-user=$TARGET_USER/" "$LIGHTDM_CONF"
    elif grep -q "^\[Seat:\*\]" "$LIGHTDM_CONF"; then
        sed -i "/^\[Seat:\*\]/a autologin-user=$TARGET_USER" "$LIGHTDM_CONF"
    else
        echo -e "\n[Seat:*]\nautologin-user=$TARGET_USER" >> "$LIGHTDM_CONF"
    fi
else
    warn "LightDM config not found — will create"
    mkdir -p "$(dirname "$LIGHTDM_CONF")"
    cat > "$LIGHTDM_CONF" << EOF
[Seat:*]
autologin-user=$TARGET_USER
EOF
fi

# Boot config
log "Configuring boot parameters"
BOOT_CONFIG="$ROOTFS/boot/firmware/config.txt"
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG="$ROOTFS/boot/config.txt"

if [ -f "$BOOT_CONFIG" ]; then
    grep -q "^gpu_mem=" "$BOOT_CONFIG" || echo "gpu_mem=128" >> "$BOOT_CONFIG"
    grep -q "^dtparam=audio=on" "$BOOT_CONFIG" || echo "dtparam=audio=on" >> "$BOOT_CONFIG"
else
    warn "config.txt not found at expected paths"
fi

# ── Phase 4: Install system packages ──────────────────────────────────────
log "Phase 4: Installing system packages (this takes a while...)"

run_in_chroot "
    apt-get update

    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        vlc \
        ffmpeg \
        python3-pip \
        python3-venv \
        unzip \
        avahi-daemon \
        libsdl2-dev \
        libsdl2-ttf-2.0-0 \
        libsdl2-image-2.0-0 \
        libsdl2-mixer-2.0-0 \
        pipewire-pulse \
        python3-pygame \
        libcap2-bin \
        usbutils \
        exfat-fuse \
        ntfs-3g

    apt-get clean
    rm -rf /var/lib/apt/lists/*
"

# ── Phase 5: Install Python packages ──────────────────────────────────────
log "Phase 5: Installing Python packages"

run_in_chroot "
    pip3 install --break-system-packages \
        'tempora==1.14.0' \
        'jaraco.functools==2.0' \
        'CherryPy==10.2.1' \
        Flask \
        pillow \
        'qrcode[pil]' \
        yt_dlp \
        psutil \
        unidecode \
        requests \
        'flask-paginate==2021.10.29' \
        'Babel==2.9.1' \
        'Flask-Babel==2.0.0'

    pip3 install --break-system-packages \
        'fastapi==0.111.0' \
        'uvicorn[standard]==0.29.0' \
        httpx

    pip3 install --break-system-packages --upgrade yt_dlp
"

# ── Phase 6: Patch VLC ────────────────────────────────────────────────────
log "Phase 6: Patching VLC binary"
run_in_chroot "
    if [ -f /usr/bin/vlc ]; then
        sed -i 's/geteuid/getppid/' /usr/bin/vlc
        echo 'VLC patched: geteuid -> getppid'
    fi
"

# ── Phase 7: Copy application code ───────────────────────────────────────
log "Phase 7: Copying application code"

DEST_PK="$ROOTFS/home/$TARGET_USER/pikaraoke"
DEST_WS="$ROOTFS/home/$TARGET_USER/pikaraoke-ws"

mkdir -p "$DEST_PK" "$DEST_WS"

rsync -a \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='.claude' \
    --exclude='image-build' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='.vscode' \
    --exclude='*.bak*' \
    --exclude='docker/' \
    --exclude='services/' \
    --exclude='pikaraoke-ws' \
    "$PIKARAOKE_SRC/" "$DEST_PK/"

rsync -a \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$PIKARAOKE_WS_SRC/" "$DEST_WS/"

chown -R 1000:1000 "$DEST_PK" "$DEST_WS"

# Ensure yt-dlp is accessible at expected path
run_in_chroot "
    YTDLP=\$(which yt-dlp 2>/dev/null || echo '')
    if [ -n \"\$YTDLP\" ]; then
        mkdir -p /home/$TARGET_USER/.local/bin
        ln -sf \"\$YTDLP\" /home/$TARGET_USER/.local/bin/yt-dlp
        chown -R 1000:1000 /home/$TARGET_USER/.local
    fi
"

# Compile translations
run_in_chroot "
    cd /home/$TARGET_USER/pikaraoke
    python3 -m babel.messages.frontend compile -d translations 2>/dev/null || true
"

# ── Phase 8: USB auto-mount ──────────────────────────────────────────────
log "Phase 8: Configuring USB auto-mount"

mkdir -p "$ROOTFS/mnt/pikaraoke-songs"

cat > "$ROOTFS/etc/systemd/system/mnt-pikaraoke\\x2dsongs.mount" << 'EOF'
[Unit]
Description=PiKaraoke Songs USB Drive
DefaultDependencies=no
After=local-fs-pre.target

[Mount]
What=LABEL=PIKARAOKE-SONGS
Where=/mnt/pikaraoke-songs
Type=auto
Options=defaults,nofail,noatime,x-systemd.device-timeout=10s

[Install]
WantedBy=multi-user.target
EOF

cat > "$ROOTFS/etc/systemd/system/mnt-pikaraoke\\x2dsongs.automount" << 'EOF'
[Unit]
Description=Automount PiKaraoke Songs USB Drive

[Automount]
Where=/mnt/pikaraoke-songs
TimeoutIdleSec=0

[Install]
WantedBy=multi-user.target
EOF

run_in_chroot "systemctl enable mnt-pikaraoke\\\\x2dsongs.automount"

# ── Phase 9: Systemd services ────────────────────────────────────────────
log "Phase 9: Creating systemd services"

cat > "$ROOTFS/etc/systemd/system/pikaraoke.service" << EOF
[Unit]
Description=PiKaraoke - Karaoke Server
After=network-online.target sound.target display-manager.service mnt-pikaraoke\\x2dsongs.automount
Wants=network-online.target mnt-pikaraoke\\x2dsongs.automount
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$TARGET_USER
Group=$TARGET_USER
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/$TARGET_USER/.Xauthority
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native
WorkingDirectory=/home/$TARGET_USER/pikaraoke
ExecStart=/usr/bin/python3 /home/$TARGET_USER/pikaraoke/app.py \\
    -y /home/$TARGET_USER/.local/bin/yt-dlp \\
    --log-level 20 \\
    --download-path /mnt/pikaraoke-songs/ \\
    --high-quality \\
    --logo-path /home/$TARGET_USER/pikaraoke/myvikaraoke.png
Restart=on-failure
RestartSec=15

[Install]
WantedBy=graphical.target
EOF

cat > "$ROOTFS/etc/systemd/system/pikaraoke-ws.service" << EOF
[Unit]
Description=PiKaraoke WS - WebSocket Bridge
After=pikaraoke.service network.target
Wants=pikaraoke.service

[Service]
Type=simple
User=$TARGET_USER
Group=$TARGET_USER
WorkingDirectory=/home/$TARGET_USER/pikaraoke-ws
Environment=XDG_RUNTIME_DIR=/run/user/1000
ExecStart=/usr/bin/python3 /home/$TARGET_USER/pikaraoke-ws/server.py \\
    --pikaraoke-url http://localhost:5000 \\
    --download-path /mnt/pikaraoke-songs/ \\
    --ytdlp-path /home/$TARGET_USER/.local/bin/yt-dlp \\
    --db-path /mnt/pikaraoke-songs/karaoke.db \\
    --port 80
Restart=on-failure
RestartSec=10
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=graphical.target
EOF

run_in_chroot "
    systemctl enable pikaraoke.service
    systemctl enable pikaraoke-ws.service
"

# ── Phase 10: Avahi/mDNS ─────────────────────────────────────────────────
log "Phase 10: Configuring mDNS"

mkdir -p "$ROOTFS/etc/avahi/services"
cat > "$ROOTFS/etc/avahi/services/pikaraoke.service" << 'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>PiKaraoke</name>
  <service>
    <type>_http._tcp</type>
    <port>80</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
EOF

run_in_chroot "systemctl enable avahi-daemon.service"

# ── Phase 11: First-boot service ─────────────────────────────────────────
log "Phase 11: Creating first-boot service"

cat > "$ROOTFS/home/$TARGET_USER/first-boot.sh" << 'FIRSTBOOT'
#!/bin/bash
LOG=/home/pikaraoke/first-boot.log
exec > "$LOG" 2>&1

echo "=== PiKaraoke First Boot $(date) ==="

# Expand filesystem to fill SD card
raspi-config --expand-rootfs 2>/dev/null || true

# Regenerate SSH host keys
rm -f /etc/ssh/ssh_host_*
dpkg-reconfigure openssh-server 2>/dev/null || true

# Create temp directories
mkdir -p /tmp/pikaraoke/extracted
chown -R pikaraoke:pikaraoke /tmp/pikaraoke

# Compile babel translations
cd /home/pikaraoke/pikaraoke
python3 -m babel.messages.frontend compile -d translations 2>/dev/null || true

# Check songs drive
if mountpoint -q /mnt/pikaraoke-songs; then
    echo "Songs drive mounted. $(ls /mnt/pikaraoke-songs/ | wc -l) items found."
else
    echo "WARNING: Songs USB drive not detected."
    echo "Insert a USB drive labeled PIKARAOKE-SONGS"
fi

# Disable this service after first run
systemctl disable pikaraoke-first-boot.service

echo "=== First boot complete ==="
FIRSTBOOT
chmod +x "$ROOTFS/home/$TARGET_USER/first-boot.sh"
chown 1000:1000 "$ROOTFS/home/$TARGET_USER/first-boot.sh"

# WiFi setup helper
cat > "$ROOTFS/home/$TARGET_USER/setup-wifi.sh" << 'WIFISETUP'
#!/bin/bash
WIFI_FILE=/boot/firmware/pikaraoke-wifi.txt
if [ -f "$WIFI_FILE" ]; then
    SSID=$(grep "^SSID=" "$WIFI_FILE" | cut -d= -f2)
    PASSWORD=$(grep "^PASSWORD=" "$WIFI_FILE" | cut -d= -f2)
    COUNTRY=$(grep "^COUNTRY=" "$WIFI_FILE" | cut -d= -f2)
    if [ -n "$SSID" ] && [ -n "$PASSWORD" ]; then
        nmcli device wifi connect "$SSID" password "$PASSWORD" 2>/dev/null || \
        nmcli connection add type wifi con-name pikaraoke-wifi \
            wifi.ssid "$SSID" wifi-sec.key-mgmt wpa-psk \
            wifi-sec.psk "$PASSWORD" 2>/dev/null
        if [ -n "$COUNTRY" ]; then
            raspi-config nonint do_wifi_country "$COUNTRY" 2>/dev/null || true
        fi
        rm -f "$WIFI_FILE"
        echo "WiFi configured: $SSID"
    fi
fi
WIFISETUP
chmod +x "$ROOTFS/home/$TARGET_USER/setup-wifi.sh"
chown 1000:1000 "$ROOTFS/home/$TARGET_USER/setup-wifi.sh"

cat > "$ROOTFS/etc/systemd/system/pikaraoke-first-boot.service" << EOF
[Unit]
Description=PiKaraoke First Boot Setup
After=network.target
Before=pikaraoke.service
ConditionPathExists=/home/$TARGET_USER/first-boot.sh

[Service]
Type=oneshot
ExecStartPre=/bin/bash /home/$TARGET_USER/setup-wifi.sh
ExecStart=/bin/bash /home/$TARGET_USER/first-boot.sh
ExecStartPost=/bin/rm -f /home/$TARGET_USER/first-boot.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

run_in_chroot "systemctl enable pikaraoke-first-boot.service"

# WiFi config placeholder on boot partition
BOOT_FW="$ROOTFS/boot/firmware"
[ -d "$BOOT_FW" ] || BOOT_FW="$ROOTFS/boot"
cat > "$BOOT_FW/pikaraoke-wifi.txt.example" << 'WIFI'
# PiKaraoke WiFi Configuration
# Rename this file to pikaraoke-wifi.txt and fill in your details.
# On first boot, the Pi will connect to this network.
#
SSID=YourNetworkName
PASSWORD=YourPassword
COUNTRY=BR
WIFI

# ── Phase 12: Performance tuning ─────────────────────────────────────────
log "Phase 12: Performance tuning for Pi 3B+"

# Swap 512MB
if [ -f "$ROOTFS/etc/dphys-swapfile" ]; then
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' "$ROOTFS/etc/dphys-swapfile"
fi

# Disable unnecessary services
run_in_chroot "
    systemctl disable bluetooth.service   2>/dev/null || true
    systemctl disable hciuart.service     2>/dev/null || true
    systemctl disable cups.service        2>/dev/null || true
    systemctl disable cups-browsed.service 2>/dev/null || true
    systemctl disable ModemManager.service 2>/dev/null || true
"

# ── Phase 12b: Boot straight into karaoke (SSH, BR, no wizard) ──────────
log "Phase 12b: Applying image fixes"
apply_pikaraoke_fixes "$ROOTFS"

# ── Phase 13: Cleanup ────────────────────────────────────────────────────
log "Phase 13: Cleanup"

rm -f "$ROOTFS/usr/sbin/policy-rc.d"

run_in_chroot "
    apt-get clean
    rm -rf /var/lib/apt/lists/*
    rm -rf /var/cache/apt/archives/*.deb
    rm -rf /tmp/*
    rm -rf /var/tmp/*
    find /home/$TARGET_USER -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    find /home/$TARGET_USER -name '.git' -type d -exec rm -rf {} + 2>/dev/null || true
"

# ── Phase 14: Unmount and compress ───────────────────────────────────────
log "Phase 14: Unmounting and compressing"

umount "$ROOTFS/tmp"
umount "$ROOTFS/sys"
umount "$ROOTFS/proc"
umount "$ROOTFS/dev/pts"
umount "$ROOTFS/dev"
umount "$ROOTFS/boot/firmware"
umount "$ROOTFS"
MOUNTS_DONE=false

# Shrink root partition
log "Shrinking root partition..."
e2fsck -f -y "${LOOP}p2" || true
resize2fs -M "${LOOP}p2"

# Calculate final image size from partition layout
PART_INFO=$(parted -s "$LOOP" unit s print 2>/dev/null | grep "^ 2")
PART_END=$(echo "$PART_INFO" | awk '{print $3}' | tr -d 's')
if [ -n "$PART_END" ]; then
    IMAGE_BYTES=$(( (PART_END + 1) * 512 + 1048576 ))  # +1MB safety margin
    losetup -d "$LOOP"
    LOOP=""
    truncate -s "$IMAGE_BYTES" "$WORK_DIR/$FINAL_IMAGE"
else
    warn "Could not determine partition end, skipping shrink"
    losetup -d "$LOOP"
    LOOP=""
fi

log "Compressing image (this takes a while)..."
cd "$WORK_DIR"
xz -9 -T0 -v "$FINAL_IMAGE"

FINAL_SIZE=$(du -sh "${FINAL_IMAGE}.xz" | cut -f1)
log "========================================="
log "Image built successfully!"
log "File: $WORK_DIR/${FINAL_IMAGE}.xz"
log "Size: $FINAL_SIZE"
log "========================================="
log ""
log "Flash to SD card with:"
log "  xz -d ${FINAL_IMAGE}.xz"
log "  sudo dd if=$FINAL_IMAGE of=/dev/sdX bs=4M status=progress"
