from .environment import setting
import os
from pathlib import Path
import signal
import sys
import threading

from .auth import Auth
from .server import Server
from .storage import Store


def main():
    store = Store(setting("DATA_DIR", "/data"),
                  setting("CACHE_DIR", "/cache"),
                  setting("RUN_DIR", "/run/ambergate"),
                  nginx_bin=os.environ.get("NGINX_BIN", "nginx"),
                  port=int(setting("HTTP_PORT", "80")),
                  control_port=int(setting("CONTROL_PORT", "8084")),
                  mime_types=os.environ.get("NGINX_MIME_TYPES", "/etc/nginx/mime.types"))
    auth = Auth(store.data)
    server = Server((setting("ADMIN_BIND", "0.0.0.0"),
                     int(setting("ADMIN_PORT", "8083"))), store, auth)
    stopping = threading.Event()
    serving = False
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    try:
        store.start()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        serving = True
        print(f"AmberGate ready. HTTP :{store.options['port']}, admin :{server.server_port}", flush=True)
        while not stopping.wait(.5):
            if store.process.poll() is not None:
                raise RuntimeError("Nginx stopped unexpectedly")
    finally:
        # Stop accepting mutations before shutting down the data plane.
        if serving:
            server.shutdown()
        server.server_close()
        with store.lock:
            store.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"AmberGate fatal: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
