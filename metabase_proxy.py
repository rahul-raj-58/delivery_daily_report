#!/usr/bin/env python3
"""
Metabase Live Proxy for Catalog SLA Dashboard
----------------------------------------------
Runs a local HTTP server on http://localhost:8765
The dashboard HTML fetches live data through this proxy.

Usage:
    python metabase_proxy.py

Requirements: Python 3.6+  (no pip installs needed)
"""

import json
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
METABASE_URL  = "https://metabase.spyne.ai"
EMAIL         = "rahul.raj@spyne.ai"
PASSWORD      = "Sonal*123@"
CARD_ID       = 12972
PORT          = 8765
CACHE_MINUTES = 10          # re-fetch from Metabase at most every N minutes
# ─────────────────────────────────────────────────────────────────────────────

_session_token  = None
_token_expires  = datetime.min
_cached_data    = None
_cache_until    = datetime.min


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def metabase_request(path, method="GET", body=None):
    """Make an authenticated request to Metabase."""
    global _session_token
    url = f"{METABASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if _session_token:
        headers["X-Metabase-Session"] = _session_token
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def authenticate():
    """Get a fresh Metabase session token."""
    global _session_token, _token_expires
    log("Authenticating with Metabase...")
    result = metabase_request(
        "/api/session",
        method="POST",
        body={"username": EMAIL, "password": PASSWORD}
    )
    _session_token = result["id"]
    _token_expires = datetime.now() + timedelta(hours=12)
    log(f"Authenticated. Token valid until {_token_expires.strftime('%H:%M')}")


def ensure_authenticated():
    if not _session_token or datetime.now() >= _token_expires:
        authenticate()


def fetch_card_data():
    """Fetch all rows from Metabase card and return as a list of dicts."""
    global _cached_data, _cache_until

    now = datetime.now()
    if _cached_data is not None and now < _cache_until:
        age = int((now - (_cache_until - timedelta(minutes=CACHE_MINUTES))).total_seconds())
        log(f"Returning cached data (age: {age}s)")
        return _cached_data

    ensure_authenticated()
    log(f"Fetching card {CARD_ID} from Metabase...")

    result = metabase_request(
        f"/api/card/{CARD_ID}/query/json",
        method="POST",
        body={"parameters": []}
    )

    _cached_data = result          # list of row dicts
    _cache_until = now + timedelta(minutes=CACHE_MINUTES)
    log(f"Fetched {len(result)} rows. Next fetch after {_cache_until.strftime('%H:%M:%S')}")
    return _cached_data


class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence default access log; we use our own

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/data":
            try:
                rows = fetch_card_data()
                payload = json.dumps({
                    "ok": True,
                    "rows": rows,
                    "fetched_at": datetime.now().isoformat(),
                    "cache_until": _cache_until.isoformat(),
                    "card_id": CARD_ID,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(payload)
                log(f"Served /data  ({len(rows)} rows)")

            except urllib.error.HTTPError as e:
                body = e.read().decode()
                log(f"Metabase error {e.code}: {body}")
                # Try re-auth once on 401
                if e.code == 401:
                    global _session_token
                    _session_token = None
                    self._send_error(503, "Session expired — retrying next request")
                else:
                    self._send_error(e.code, f"Metabase error: {body[:200]}")

            except Exception as ex:
                log(f"Error: {ex}")
                self._send_error(500, str(ex))

        elif parsed.path == "/health":
            msg = json.dumps({"ok": True, "port": PORT}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(msg)

        else:
            self._send_error(404, "Not found. Use /data or /health")

    def _send_error(self, code, message):
        body = json.dumps({"ok": False, "error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)


def main():
    print("=" * 56)
    print("  Metabase Live Proxy — Catalog SLA Dashboard")
    print(f"  Listening on http://localhost:{PORT}")
    print(f"  Metabase: {METABASE_URL}")
    print(f"  Card ID:  {CARD_ID}")
    print(f"  Cache:    {CACHE_MINUTES} minutes")
    print("  Press Ctrl+C to stop")
    print("=" * 56)

    # Authenticate eagerly so any credential issue surfaces immediately
    try:
        authenticate()
    except Exception as e:
        print(f"\n❌  Authentication failed: {e}")
        print("    Check EMAIL / PASSWORD in this script and try again.\n")
        return

    server = HTTPServer(("localhost", PORT), ProxyHandler)
    print(f"\n✅  Ready — open catalog-sla-dashboard.html in your browser\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")


if __name__ == "__main__":
    main()
