"""Servidor mínimo: serve brazil-matches.json e atualiza em background.

Render → Web Service → Start Command:  python server.py
Render → Build Command:                pip install -r requirements.txt

Comportamento:
- GET /                     → brazil-matches.json (CORS *)
- GET /health               → ok
- A cada 60s verifica a idade do JSON; se > REFRESH_MIN (default15), roda
  o scraper em subprocess (thread, nunca sobrepõe runs) → atualização em
  runtime, SEM commit/deploy.
- O scraper tem seu próprio rate limit (20 chamadas,1 req/2 min).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JSON_PATH = ROOT / "brazil-matches.json"
SCRAPER = ROOT / "hltv" / "scraper.py"
REFRESH_MIN = int(os.environ.get("REFRESH_MIN", "15"))
PORT = int(os.environ.get("PORT", "8080"))

_scrape_lock = threading.Lock()


def run_scraper() -> None:
    """Roda o scraper (rate-limited por ele mesmo). Nunca sobrepõe runs."""
    if not _scrape_lock.acquire(blocking=False):
        return
    try:
        print(f"[refresh] {time.strftime('%Y-%m-%d %H:%M:%S')} iniciando scraper ...", flush=True)
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(SCRAPER), "--out", str(JSON_PATH)],
            cwd=ROOT,
            timeout=2400,  # ~12 chamadas ×2 min + folga
            capture_output=False,
        )
        print(
            f"[refresh] exit={proc.returncode} em {time.monotonic() - t0:.0f}s",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001 - servidor segue vivo em qualquer falha
        print(f"[refresh] erro: {e}", flush=True)
    finally:
        _scrape_lock.release()


def refresh_loop() -> None:
    """Verifica a cada60s se o JSON está velho (> REFRESH_MIN)."""
    while True:
        try:
            if JSON_PATH.exists():
                age = time.time() - JSON_PATH.stat().st_mtime
                if age >= REFRESH_MIN * 60:
                    run_scraper()
                    continue  # recheca imediatamente após o run
                time.sleep(min(60, REFRESH_MIN * 60 - age))
                continue
            # sem JSON ainda (repo novinho): baixa na primeira chance
            run_scraper()
        except Exception as e:  # noqa: BLE001
            print(f"[refresh] loop erro: {e}", flush=True)
        time.sleep(60)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")  # sem CDN/espera: sempre o estado atual
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - API do http.server
        path = self.path.split("?", 1)[0]
        if path in ("/", "/brazil-matches.json"):
            if JSON_PATH.exists():
                self._send(200, JSON_PATH.read_bytes(), "application/json; charset=utf-8")
            else:
                self._send(503, b'{"error":"json ainda nao gerado"}', "application/json")
        elif path == "/health":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def log_message(self, format: str, *args) -> None:  # log enxuto
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> None:
    if not SCRAPER.exists():
        raise SystemExit(f"scraper nao encontrado: {SCRAPER}")
    threading.Thread(target=refresh_loop, daemon=True, name="refresh").start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"[server] porta {PORT} | refresh a cada {REFRESH_MIN}min | "
        f"json={JSON_PATH.name}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
