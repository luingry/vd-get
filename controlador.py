#!/usr/bin/env python3
"""
Painel HTTP local: serve index.html e expõe API para ligar / parar / reiniciar servidor.py (WebSocket).
Uso: python controlador.py  →  abra http://127.0.0.1:8764/
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(DIR, "index.html")
SERVIDOR_PATH = os.path.join(DIR, "servidor.py")
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 8764

_worker = None
_lock = threading.Lock()


def _popen_kwargs():
    kw = {
        "args": [sys.executable, SERVIDOR_PATH],
        "cwd": DIR,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return kw


def _spawn_worker_locked():
    global _worker
    _worker = subprocess.Popen(**_popen_kwargs())


def api_status():
    with _lock:
        running = _worker is not None and _worker.poll() is None
        pid = _worker.pid if running else None
    return {"ok": True, "running": running, "pid": pid}


def api_start():
    global _worker
    with _lock:
        if _worker is not None and _worker.poll() is None:
            return {"ok": True, "running": True, "message": "já em execução"}
        _spawn_worker_locked()
        return {"ok": True, "running": True, "message": "iniciado"}


def api_stop():
    global _worker
    with _lock:
        if _worker is None or _worker.poll() is not None:
            _worker = None
            return {"ok": True, "running": False, "message": "já estava parado"}
        proc = _worker
        _worker = None
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    return {"ok": True, "running": False, "message": "encerrado"}


def api_restart():
    api_stop()
    return api_start()


class Handler(BaseHTTPRequestHandler):
    server_version = "VDGET-Control/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s — %s" % (self.log_date_time_string(), self.address_string(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _plain(self, code, text):
        raw = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._json(200, api_status())
            return
        if path in ("/", "/index.html"):
            if not os.path.isfile(INDEX_PATH):
                self._plain(404, "index.html não encontrado")
                return
            with open(INDEX_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return
        self._plain(404, "Not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/start":
            self._json(200, api_start())
            return
        if path == "/api/stop":
            self._json(200, api_stop())
            return
        if path == "/api/restart":
            self._json(200, api_restart())
            return
        self._plain(404, "Not found")


def main():
    if not os.path.isfile(SERVIDOR_PATH):
        print("[ERRO] servidor.py não encontrado em:", DIR)
        sys.exit(1)
    api_start()
    httpd = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), Handler)
    print(
        f"\n  VDGET — controle: http://{CONTROL_HOST}:{CONTROL_PORT}/\n"
        f"  WebSocket (servidor.py): ws://localhost:8765\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Encerrando…")
    finally:
        api_stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
