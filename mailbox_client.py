"""Instance-bound HTTP client. Credentials stay in a private file, never command arguments."""

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def valid_mailbox_url(value):
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        _ = parsed.port
    except ValueError:
        return False
    if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--body-file", help="JSON payload; otherwise POST/PATCH read JSON from stdin")
    parser.add_argument("method", choices=["GET", "POST", "PATCH", "DELETE"])
    parser.add_argument("path", help="Mailbox path, e.g. /inbox/summary")
    args = parser.parse_args()
    config = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8-sig"))
    if not valid_mailbox_url(config["url"]) or not args.path.startswith("/") or args.path.startswith("//"):
        raise ValueError("Use HTTPS or a loopback-only local URL and a path on this mailbox")
    body = None
    if args.method in ("POST", "PATCH"):
        content = Path(args.body_file).read_text(encoding="utf-8-sig") if args.body_file else sys.stdin.read()
        body = json.dumps(json.loads(content or "{}"), ensure_ascii=False).encode("utf-8")
    request = Request(config["url"].rstrip("/") + args.path, method=args.method, data=body,
                      headers={"Authorization": "Bearer " + config["token"],
                               "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            print(json.dumps(json.load(response), ensure_ascii=False))
        return 0
    except HTTPError as error:
        print(json.dumps({"error": error.code, "detail": error.read().decode("utf-8", errors="replace")}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
