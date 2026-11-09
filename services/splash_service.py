"""
Splash Screen Service — standalone HTTP API for pygame TV display.

Runs inside the pikaraoke-splash container with access to the host display.
Receives state updates from the web container and renders on the TV.
"""

import logging
import os
import sys
import threading
import time

from flask import Flask, jsonify, request

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.DEBUG,
)

app = Flask(__name__)

SERVICE_PORT = int(os.environ.get("SERVICE_PORT", 5004))
LOGO_PATH = os.environ.get("LOGO_PATH", "/app/myvikaraoke.png")
QR_URL = os.environ.get("QR_URL", "http://myvikaraoke.local")
HIDE_IP = os.environ.get("HIDE_IP", "false").lower() == "true"

# Pygame must be initialized on the main thread
import pygame
import qrcode

COLORS = {
    'white': (255, 255, 255),
    'blue': (10, 114, 186),
    'green': (34, 154, 91),
}


class SplashDisplay:
    """Manages the pygame display on the main thread."""

    def __init__(self):
        self.screen = None
        self.font = None
        self.width = 0
        self.height = 0
        self._cached_logo = None
        self._cached_qr = None
        self.qr_code_path = "/tmp/pikaraoke/qrcode.png"

        # State (updated by API calls)
        self.now_playing = None
        self.now_playing_user = None
        self.queue = []
        self.transpose_progress = 0
        self.transpose_semitones = 0
        self.is_transposing = False
        self._toasts = []
        self._needs_redraw = True

    def initialize(self):
        os.makedirs("/tmp/pikaraoke", exist_ok=True)
        self._generate_qr_code()

        os.environ.setdefault("SDL_VIDEO_WINDOW_POS", "0,0")
        pygame.display.init()
        pygame.display.set_caption("pikaraoke")
        pygame.font.init()
        pygame.mouse.set_visible(0)
        self.font = pygame.font.SysFont(pygame.font.get_default_font(), 40)

        info = pygame.display.Info()
        self.width = info.current_w
        self.height = info.current_h
        logging.info("Display: %dx%d" % (self.width, self.height))

        self.screen = pygame.display.set_mode(
            [self.width, self.height], pygame.NOFRAME
        )
        pygame.display.toggle_fullscreen()
        self._render_splash()

    def _generate_qr_code(self):
        qr = qrcode.QRCode(version=1, box_size=1, border=4)
        qr.add_data(QR_URL)
        qr.make()
        img = qr.make_image()
        img.save(self.qr_code_path)

    def _render_splash(self):
        if not self.screen:
            return
        self.screen.fill((0, 0, 0))

        if self._cached_logo is None:
            try:
                self._cached_logo = pygame.image.load(LOGO_PATH)
            except Exception as e:
                logging.error("Logo load error: %s" % e)
                self._cached_logo = pygame.Surface((200, 200))
        if self._cached_qr is None:
            try:
                self._cached_qr = pygame.transform.scale(
                    pygame.image.load(self.qr_code_path), (150, 150)
                )
            except Exception:
                self._cached_qr = pygame.Surface((150, 150))

        logo_rect = self._cached_logo.get_rect(center=self.screen.get_rect().center)
        self.screen.blit(self._cached_logo, logo_rect)

        blitY = self.screen.get_rect().bottomleft[1] - 40
        self.screen.blit(self._cached_qr, (20, blitY - 125))

        if not HIDE_IP:
            text = self.font.render(
                "Connect at: %s" % QR_URL, True, (255, 255, 255)
            )
        else:
            text = self.font.render("", True, (255, 255, 255))
        self.screen.blit(text, (self._cached_qr.get_width() + 35, blitY))

        self._render_queue_overlay()
        self._render_toasts()
        pygame.display.update()

    def _render_queue_overlay(self):
        if not self.screen:
            return
        font_title = pygame.font.SysFont(pygame.font.get_default_font(), 30)
        font_small = pygame.font.SysFont(pygame.font.get_default_font(), 20)
        font_item = pygame.font.SysFont(pygame.font.get_default_font(), 24)

        x, y = 20, 15
        max_y = self.height - 200

        header = font_title.render("Fila", True, (255, 255, 255))
        self.screen.blit(header, (x, y))
        y += 40

        if self.now_playing:
            title = self.now_playing
            if len(title) > 38:
                title = title[:38] + "..."
            ts = font_item.render(u"\u25B6 " + title, True, (34, 154, 91))
            self.screen.blit(ts, (x, y))
            y += font_item.get_linesize()
            if self.now_playing_user:
                us = font_small.render("   " + self.now_playing_user, True, (180, 180, 180))
                self.screen.blit(us, (x, y))
                y += font_small.get_linesize() + 6

        for i, item in enumerate(self.queue):
            if y > max_y:
                remaining = len(self.queue) - i
                more = font_small.render("   + %d mais..." % remaining, True, (120, 120, 120))
                self.screen.blit(more, (x, y))
                break
            title = item.get("title", "")
            if len(title) > 38:
                title = title[:38] + "..."
            ts = font_item.render("%d. %s" % (i + 1, title), True, (220, 220, 220))
            self.screen.blit(ts, (x, y))
            y += font_item.get_linesize()
            user = item.get("user", "")
            if user:
                us = font_small.render("   " + user, True, (140, 140, 140))
                self.screen.blit(us, (x, y))
                y += font_small.get_linesize() + 4

    def _render_toasts(self):
        if not self.screen:
            return
        now = time.time()
        self._toasts = [t for t in self._toasts if t["expires"] > now]
        if not self._toasts:
            return
        font_toast = pygame.font.SysFont(pygame.font.get_default_font(), 28)
        y = 10
        for toast in self._toasts:
            text_surface = font_toast.render(toast["text"], True, (255, 255, 255))
            tw = text_surface.get_width() + 20
            th = text_surface.get_height() + 12
            tx = self.width - tw - 10
            bg = pygame.Surface((tw, th), pygame.SRCALPHA)
            bg.fill((0, 0, 0, 180))
            self.screen.blit(bg, (tx, y))
            self.screen.blit(text_surface, (tx + 10, y + 6))
            y += th + 5

    def _render_transpose_overlay(self):
        if not self.screen:
            return
        self.screen.fill((0, 0, 0))
        song = self.now_playing or ""
        semitones = self.transpose_semitones
        progress = self.transpose_progress

        title = self.font.render(song, True, (255, 255, 255))
        title_rect = title.get_rect(centerx=self.width // 2, y=self.height // 3 - 60)
        self.screen.blit(title, title_rect)

        sub_font = pygame.font.SysFont(pygame.font.get_default_font(), 36)
        sign = "+" if semitones > 0 else ""
        sub = sub_font.render("Changing key: %s%d semitones" % (sign, semitones), True, (255, 200, 80))
        sub_rect = sub.get_rect(centerx=self.width // 2, y=self.height // 3)
        self.screen.blit(sub, sub_rect)

        bar_width = self.width // 2
        bar_height = 40
        bar_x = (self.width - bar_width) // 2
        bar_y = self.height // 2
        pygame.draw.rect(self.screen, (80, 80, 80), (bar_x, bar_y, bar_width, bar_height))
        fill_w = int(bar_width * progress / 100)
        if fill_w > 0:
            pygame.draw.rect(self.screen, (34, 154, 91), (bar_x, bar_y, fill_w, bar_height))
        pygame.draw.rect(self.screen, (255, 255, 255), (bar_x, bar_y, bar_width, bar_height), 2)

        pct = self.font.render("%d%%" % progress, True, (255, 255, 255))
        pct_rect = pct.get_rect(centerx=self.width // 2, y=bar_y + bar_height + 15)
        self.screen.blit(pct, pct_rect)
        pygame.display.update()

    def _render_next_song(self, title, user):
        if not self.screen:
            return
        self._render_splash()

        font_header = pygame.font.SysFont(pygame.font.get_default_font(), 44)
        header = font_header.render("Up next", True, COLORS['blue'])
        header_x = self.width - header.get_width() - 20
        self.screen.blit(header, (header_x, 8))

        font_song = pygame.font.SysFont(pygame.font.get_default_font(), 36)
        display_title = title if len(title) <= 50 else title[:50] + "..."
        title_surface = font_song.render(display_title, True, COLORS['green'])
        title_x = self.width - title_surface.get_width() - 20
        self.screen.blit(title_surface, (title_x, 55))

        font_user = pygame.font.SysFont(pygame.font.get_default_font(), 28)
        user_surface = font_user.render(user, True, COLORS['white'])
        user_x = self.width - user_surface.get_width() - 20
        self.screen.blit(user_surface, (user_x, 95))
        pygame.display.update()

    def run_loop(self):
        """Main event loop — called repeatedly from the main thread."""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    return False

        if self._needs_redraw:
            if self.is_transposing:
                self._render_transpose_overlay()
            else:
                self._render_splash()
            self._needs_redraw = False

        pygame.time.wait(500)
        return True


display = SplashDisplay()


# --- API Routes (called from Flask threads) ---

@app.route("/health")
def health():
    return jsonify({"status": "ok", "display_active": display.screen is not None})


@app.route("/update_state", methods=["POST"])
def update_state():
    """Update the display state. Body: {"now_playing": ..., "queue": [...], ...}"""
    data = request.get_json(force=True)

    changed = False
    if "now_playing" in data and data["now_playing"] != display.now_playing:
        display.now_playing = data["now_playing"]
        changed = True
    if "now_playing_user" in data and data["now_playing_user"] != display.now_playing_user:
        display.now_playing_user = data["now_playing_user"]
        changed = True
    if "queue" in data:
        display.queue = data["queue"]
        changed = True
    if "is_transposing" in data:
        display.is_transposing = data["is_transposing"]
        display.transpose_progress = data.get("transpose_progress", 0)
        display.transpose_semitones = data.get("transpose_semitones", 0)
        changed = True

    if changed:
        display._needs_redraw = True

    return jsonify({"status": "ok"})


@app.route("/show_next_song", methods=["POST"])
def show_next_song():
    """Show 'Up next' screen. Body: {"title": "...", "user": "..."}"""
    data = request.get_json(force=True)
    title = data.get("title", "")
    user = data.get("user", "")
    display._needs_redraw = True
    # Store for rendering on main thread
    display._next_song_pending = {"title": title, "user": user}
    return jsonify({"status": "ok"})


@app.route("/toast", methods=["POST"])
def toast():
    """Show a toast notification. Body: {"text": "...", "duration": 5}"""
    data = request.get_json(force=True)
    text = data.get("text", "")
    duration = data.get("duration", 5)
    display._toasts.append({
        "text": text,
        "expires": time.time() + duration,
    })
    display._needs_redraw = True
    return jsonify({"status": "ok"})


@app.route("/render_splash", methods=["POST"])
def render_splash():
    display._needs_redraw = True
    return jsonify({"status": "ok"})


def run_flask_in_thread():
    """Start Flask in a background thread so main thread can run pygame."""
    app.run(host="0.0.0.0", port=SERVICE_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    logging.info("Starting Splash Service on port %d" % SERVICE_PORT)

    # Start Flask API in background thread
    flask_thread = threading.Thread(target=run_flask_in_thread, daemon=True)
    flask_thread.start()

    # Initialize pygame on main thread
    display.initialize()

    # Main event loop
    running = True
    while running:
        try:
            # Check if there's a pending next-song render
            pending = getattr(display, '_next_song_pending', None)
            if pending:
                display._render_next_song(pending["title"], pending["user"])
                display._next_song_pending = None
            else:
                running = display.run_loop()
        except KeyboardInterrupt:
            running = False

    pygame.quit()
