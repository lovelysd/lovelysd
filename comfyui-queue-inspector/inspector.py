"""ComfyUI Queue Inspector - entry point.

Usage:
    python inspector.py [--comfy http://127.0.0.1:8188] [--port 8190] [--no-browser]
"""

import os
import sys

# ComfyUI portable's embedded Python uses a ._pth file, which disables adding
# the script's own folder to sys.path. Add it explicitly so `qi` imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import threading  # noqa: E402
import webbrowser  # noqa: E402

from qi import __version__  # noqa: E402
from qi.comfy_client import ComfyClient  # noqa: E402
from qi.http_server import serve  # noqa: E402
from qi.service import QueueService  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only viewer for the live ComfyUI queue.")
    p.add_argument("--comfy", default=os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188"),
                   help="ComfyUI address (default: http://127.0.0.1:8188)")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default: 127.0.0.1 = this PC only)")
    p.add_argument("--port", type=int, default=8190, help="port for the inspector page (default: 8190)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = p.parse_args(argv)

    comfy = args.comfy if "://" in args.comfy else "http://" + args.comfy
    service = QueueService(ComfyClient(comfy))
    service.start()

    try:
        httpd = serve(service, args.host, args.port)
    except OSError as exc:
        print(f"Cannot listen on {args.host}:{args.port}: {exc}")
        print("Another program may be using this port. Try --port 8191.")
        return 1

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{shown_host}:{args.port}/"
    print(f"ComfyUI Queue Inspector {__version__}")
    print(f"  ComfyUI : {comfy}")
    print(f"  Open    : {url}")
    print("  Stop    : Ctrl+C")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
