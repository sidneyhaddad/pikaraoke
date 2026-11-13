#!/usr/bin/env python3
"""
pikaraoke-wifi-portal — Wi-Fi setup hotspot for PiKaraoke.

When wlan0 is not connected to any Wi-Fi network, the Pi opens an open
hotspot (SSID below). Connecting a phone to it pops up a captive page that
lists nearby networks; picking one and typing the password saves it in
NetworkManager and the Pi joins it. If joining fails, the hotspot comes back
with an error message. Ethernet is never touched.

Runs as root (systemd: pikaraoke-wifi-portal.service).
"""
import html
import http.server
import logging
import socketserver
import subprocess
import threading
import time
import urllib.parse

HOTSPOT_SSID = "myviKaraoke-Config"
HOTSPOT_CON = "pikaraoke-hotspot"
HOTSPOT_IP = "10.42.0.1"
IFACE = "wlan0"
PORTAL_PORT = 8888
BOOT_GRACE = 45          # seconds to let saved Wi-Fi connect before opening the hotspot
CHECK_EVERY = 20
CONNECT_TIMEOUT = 100
CONNECT_ATTEMPTS = 3
KARAOKE_URL = "http://myvikaraoke.local/"

logging.basicConfig(level=logging.INFO, format="[wifi-portal] %(message)s")
log = logging.info

state = {"networks": [], "error": "", "busy": False, "nat": False}
lock = threading.Lock()


def run(*cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def nm_ready():
    return run("nmcli", "-t", "general", "status")[0] == 0


def ensure_radio():
    run("rfkill", "unblock", "wifi")
    run("nmcli", "radio", "wifi", "on")


def wifi_state():
    """Returns (connected_to_client_wifi, hotspot_active)."""
    rc, out = run("nmcli", "-t", "-e", "no", "-f", "DEVICE,STATE,CONNECTION", "device")
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == IFACE:
            st, con = parts[1], parts[2]
            if st.startswith("connected") or st == "connected":
                return (con != HOTSPOT_CON, con == HOTSPOT_CON)
            return (False, False)
    return (False, False)


def scan():
    run("nmcli", "device", "wifi", "rescan", "ifname", IFACE, timeout=20)
    time.sleep(4)
    rc, out = run("nmcli", "-t", "-e", "no", "-f", "SIGNAL,SECURITY,SSID",
                  "device", "wifi", "list", "ifname", IFACE)
    best = {}
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[2] or parts[2] == HOTSPOT_SSID:
            continue
        sig = int(parts[0]) if parts[0].isdigit() else 0
        ssid, sec = parts[2], parts[1]
        if ssid not in best or best[ssid][0] < sig:
            best[ssid] = (sig, sec)
    nets = sorted(((s, v[0], v[1]) for s, v in best.items()), key=lambda x: -x[1])
    if nets:
        state["networks"] = nets
    log(f"scan: {len(nets)} networks")


PORTAL_RULES = f"""
table ip pkportal {{
    chain pre {{
        type nat hook prerouting priority -100; policy accept;
        iifname "{IFACE}" udp dport 53 redirect to :53
        iifname "{IFACE}" tcp dport 53 redirect to :53
        iifname "{IFACE}" tcp dport 80 redirect to :{PORTAL_PORT}
    }}
    chain block_forward {{
        type filter hook forward priority -10; policy accept;
        iifname "{IFACE}" drop
    }}
}}
"""


def nat_redirect(on):
    """Captive portal rules: hotspot clients get no internet (so phones show
    the sign-in page), all their DNS goes to the Pi and HTTP to the portal."""
    run("nft", "delete", "table", "ip", "pkportal")
    state["nat"] = False
    if on:
        try:
            r = subprocess.run(["nft", "-f", "-"], input=PORTAL_RULES, text=True,
                               capture_output=True, timeout=10)
            state["nat"] = r.returncode == 0
            if r.returncode:
                log(f"nft failed: {r.stderr.strip()}")
        except Exception as e:
            log(f"nft error: {e}")


def hotspot_up():
    ensure_radio()
    scan()
    run("nmcli", "connection", "delete", HOTSPOT_CON)
    run("nmcli", "connection", "add", "type", "wifi", "ifname", IFACE,
        "con-name", HOTSPOT_CON, "autoconnect", "no", "ssid", HOTSPOT_SSID,
        "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
        "ipv4.method", "shared", "ipv4.addresses", f"{HOTSPOT_IP}/24",
        "ipv6.method", "disabled")
    rc, out = run("nmcli", "connection", "up", HOTSPOT_CON, timeout=40)
    if rc == 0:
        nat_redirect(True)
        log(f"hotspot '{HOTSPOT_SSID}' up")
    else:
        log(f"hotspot failed: {out}")


def hotspot_down():
    nat_redirect(False)
    run("nmcli", "connection", "down", HOTSPOT_CON, timeout=20)
    log("hotspot down")


def reset_radio():
    """Leave AP mode cleanly: cycle the Wi-Fi radio and rescan."""
    run("nmcli", "radio", "wifi", "off")
    time.sleep(2)
    ensure_radio()
    time.sleep(3)
    run("nmcli", "device", "wifi", "rescan", "ifname", IFACE, timeout=20)
    time.sleep(5)


def try_connect(ssid, password):
    con = f"wifi-{ssid}"[:60]
    with lock:
        state["busy"] = True
    try:
        hotspot_down()
        run("nmcli", "connection", "delete", con)
        cmd = ["nmcli", "connection", "add", "type", "wifi", "ifname", IFACE,
               "con-name", con, "ssid", ssid, "connection.autoconnect", "yes",
               "connection.autoconnect-priority", "10", "ipv4.dhcp-timeout", "90"]
        if password:
            cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        rc, out = run(*cmd)
        if rc == 0:
            # Right after AP mode the brcmfmac radio often fails to tune
            # ("set chanspec fail", ASSOC-REJECT) and NetworkManager reports
            # it as missing secrets. Reset the radio and retry a few times.
            for attempt in range(1, CONNECT_ATTEMPTS + 1):
                reset_radio()
                rc, out = run("nmcli", "--wait", str(CONNECT_TIMEOUT - 5), "connection", "up", con,
                              timeout=CONNECT_TIMEOUT)
                if rc == 0:
                    break
                log(f"connect to '{ssid}' attempt {attempt} failed: {out.splitlines()[-1] if out else ''}")
        if rc == 0:
            log(f"connected to '{ssid}'")
            state["error"] = ""
            return
        log(f"connect to '{ssid}' failed: {out}")
        run("nmcli", "connection", "delete", con)
        state["error"] = (f"Não foi possível conectar em \"{ssid}\". "
                          "Confira a senha e tente de novo.")
        hotspot_up()
    finally:
        with lock:
            state["busy"] = False


# ── Web page ───────────────────────────────────────────────────────────────
STYLE = """
body{font-family:system-ui,-apple-system,sans-serif;background:#111;color:#eee;margin:0;padding:16px}
.card{max-width:440px;margin:0 auto;background:#1c1c1c;border-radius:12px;padding:20px}
h1{font-size:1.3em;margin:0 0 4px}h1 b{color:#1e88e5}h1 i{color:#2e9e55;font-style:normal}
p{color:#bbb}label.net{display:flex;justify-content:space-between;align-items:center;
padding:12px;border:1px solid #333;border-radius:8px;margin:6px 0;cursor:pointer}
label.net:has(input:checked){border-color:#1e88e5;background:#14263a}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:12px;
border-radius:8px;border:1px solid #444;background:#111;color:#eee;font-size:1em;margin:6px 0}
button{width:100%;padding:14px;border:0;border-radius:8px;background:#1e88e5;color:#fff;
font-size:1.05em;margin-top:10px}.err{background:#4a1515;color:#ffb4b4;padding:10px;border-radius:8px}
small{color:#888}a{color:#5aa9f0}
"""


def page_form():
    nets = state["networks"]
    rows = "".join(
        f'<label class="net"><span><input type="radio" name="ssid" value="{html.escape(s, True)}"'
        f'{" checked" if i == 0 else ""}> {html.escape(s)}</span>'
        f'<small>{"🔒 " if sec and sec != "--" else ""}{sig}%</small></label>'
        for i, (s, sig, sec) in enumerate(nets))
    if not rows:
        rows = "<p>Nenhuma rede encontrada. Digite o nome abaixo.</p>"
    err = f'<p class="err">{html.escape(state["error"])}</p>' if state["error"] else ""
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Configurar Wi-Fi</title><style>{STYLE}</style></head><body><div class="card">
<h1><b>myvi</b><i>Karaokê</i> · Wi-Fi</h1>
<p>Escolha a rede Wi-Fi que o karaokê vai usar.</p>{err}
<form method="post" action="/connect">{rows}
<input type="text" name="other" placeholder="Outra rede (nome exato)" autocapitalize="none" autocorrect="off">
<input type="password" name="password" placeholder="Senha do Wi-Fi">
<button type="submit">Conectar</button></form>
<p><small>Sem Wi-Fi? O karaokê também funciona aqui mesmo:
<a href="http://{HOTSPOT_IP}:5000/">abrir karaokê</a></small></p>
</div></body></html>"""


def page_connecting(ssid):
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conectando…</title><style>{STYLE}</style></head><body><div class="card">
<h1><b>myvi</b><i>Karaokê</i> · Wi-Fi</h1>
<p>Conectando o karaokê em <b>{html.escape(ssid)}</b>…</p>
<p>Esta rede de configuração vai desligar. Conecte seu celular em
<b>{html.escape(ssid)}</b> e abra <a href="{KARAOKE_URL}">{KARAOKE_URL}</a>.</p>
<p><small>Se a senha estiver errada, a rede <b>{HOTSPOT_SSID}</b> volta em alguns minutos
com uma mensagem de erro.</small></p></div></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body="", headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        # Any captive-probe / foreign host -> redirect to the portal itself
        if host not in (HOTSPOT_IP, "") or self.path != "/":
            return self._send(302, "", {"Location": f"http://{HOTSPOT_IP}/"})
        self._send(200, page_form())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace"))
        ssid = (form.get("other", [""])[0] or form.get("ssid", [""])[0]).strip()
        password = form.get("password", [""])[0]
        if not ssid:
            state["error"] = "Escolha uma rede."
            return self._send(302, "", {"Location": f"http://{HOTSPOT_IP}/"})
        if password and not 8 <= len(password) <= 63:
            state["error"] = "A senha do Wi-Fi precisa ter de 8 a 63 caracteres."
            return self._send(302, "", {"Location": f"http://{HOTSPOT_IP}/"})
        self._send(200, page_connecting(ssid))
        threading.Timer(3, try_connect, args=(ssid, password)).start()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    for _ in range(60):
        if nm_ready():
            break
        time.sleep(2)
    ensure_radio()
    threading.Thread(target=Server(("0.0.0.0", PORTAL_PORT), Handler).serve_forever,
                     daemon=True).start()
    log(f"portal listening on :{PORTAL_PORT}")
    if wifi_state()[1]:
        nat_redirect(True)            # hotspot already up (service restart)
    else:
        time.sleep(BOOT_GRACE)
    while True:
        try:
            with lock:
                busy = state["busy"]
            if not busy:
                client, hotspot = wifi_state()
                if client:
                    if state.get("nat"):
                        nat_redirect(False)
                elif hotspot:
                    if not state.get("nat"):   # e.g. service restarted while hotspot was up
                        nat_redirect(True)
                else:
                    hotspot_up()
        except Exception as e:  # keep the loop alive no matter what
            log(f"loop error: {e}")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
