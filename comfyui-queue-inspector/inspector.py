"""ComfyUI Queue Inspector - entry point.

Usage:
    python inspector.py [--comfy http://127.0.0.1:8188] [--port 8190] [--no-browser]

Without --comfy (and without the COMFYUI_URL environment variable) the
inspector tries the usual local ComfyUI addresses: 8188 (portable / manual
install) and 8000 (ComfyUI Desktop app), and keeps retrying while neither
answers, so ComfyUI may be started before or after the inspector.
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
from qi.comfy_client import ComfyClient, ComfyUnavailable  # noqa: E402
from qi.http_server import serve  # noqa: E402
from qi.service import QueueService  # noqa: E402


DEFAULT_COMFY_URLS = ["http://127.0.0.1:8188", "http://127.0.0.1:8000"]


def pick_comfy_url(candidates):
    """Return the first candidate that answers, else the first candidate."""
    for url in candidates:
        try:
            ComfyClient(url, timeout=1.0).get_queue_remaining()
            return url
        except ComfyUnavailable:
            continue
    return candidates[0]


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only viewer for the live ComfyUI queue.")
    p.add_argument("--comfy", default=os.environ.get("COMFYUI_URL") or None,
                   help="ComfyUI address (default: auto-detect 127.0.0.1:8188, then :8000)")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default: 127.0.0.1 = this PC only)")
    p.add_argument("--port", type=int, default=8190, help="port for the inspector page (default: 8190)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = p.parse_args(argv)

    if args.comfy:
        candidates = [args.comfy if "://" in args.comfy else "http://" + args.comfy]
    else:
        candidates = DEFAULT_COMFY_URLS
    comfy = pick_comfy_url(candidates)
    service = QueueService(ComfyClient(comfy), candidates=candidates)
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
    if len(candidates) > 1:
        print(f"  ComfyUI : {comfy} (auto-detect: {', '.join(candidates)})")
    else:
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
