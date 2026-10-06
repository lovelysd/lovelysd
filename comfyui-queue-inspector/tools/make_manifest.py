"""Regenerate the file manifest inside tools/check_windows.ps1.

    python tools/make_manifest.py [--old-rev <git revision of the previous release>]

For every release file it records SHA256 hashes of the LF and CRLF variants
(a copy may have been converted on the way to Windows). With --old-rev, the
hashes of that release are recorded too, so the check can say "older
version" instead of "differs".
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qi import __version__  # noqa: E402

PS1 = os.path.join(ROOT, "tools", "check_windows.ps1")
SKIP = {"tools/check_windows.ps1", "check_inspector.bat"}  # cannot contain its own hash
BINARY_EXT = (".png",)


def files():
    out = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "."],
                                  cwd=ROOT).decode().split("\n")
    return sorted(f for f in out if f and f not in SKIP and "__pycache__" not in f)


def required(path):
    return path in ("inspector.py", "start_inspector.bat") or path.startswith(("qi/", "web/"))


def why(path):
    if required(path):
        return "required"
    if path.startswith("tests/"):
        return "tests only"
    if path.startswith("tools/"):
        return "developer tool"
    return "documentation"


def variants(data, path):
    if path.endswith(BINARY_EXT):
        return [data]
    lf = data.replace(b"\r\n", b"\n")
    return [lf, lf.replace(b"\n", b"\r\n")]


def hashes(data, path):
    return sorted({hashlib.sha256(v).hexdigest().upper() for v in variants(data, path)})


def old_blob(rev, path):
    try:
        return subprocess.check_output(["git", "show", "%s:./%s" % (rev, path)], cwd=ROOT,
                                       stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-rev")
    args = ap.parse_args()

    lines = ["$version = '%s'" % __version__, "$manifest = @("]
    for path in files():
        with open(os.path.join(ROOT, path), "rb") as f:
            cur = hashes(f.read(), path)
        old = []
        if args.old_rev:
            blob = old_blob(args.old_rev, path)
            if blob is not None:
                old = [h for h in hashes(blob, path) if h not in cur]
        lines.append("    @{ p='%s'; req=$%s; why='%s'; cur=@(%s); old=@(%s) }" % (
            path, "true" if required(path) else "false", why(path),
            ",".join("'%s'" % h for h in cur), ",".join("'%s'" % h for h in old)))
    lines.append(")")
    block = "\n".join(lines)

    with open(PS1, encoding="ascii") as f:
        text = f.read()
    text, n = re.subn(r"(# BEGIN MANIFEST[^\n]*\n).*?(\n# END MANIFEST)",
                      lambda m: m.group(1) + block + m.group(2), text, flags=re.S)
    if n != 1:
        sys.exit("manifest markers not found in " + PS1)
    with open(PS1, "w", encoding="ascii", newline="\n") as f:
        f.write(text)
    print("manifest: %d files, version %s" % (len(files()), __version__))


if __name__ == "__main__":
    main()
