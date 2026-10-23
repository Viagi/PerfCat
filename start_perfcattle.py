"""PerfCattle launcher used by the project and desktop shortcuts.

If a PerfCattle instance is already answering on the target port, open its page
instead of starting a second server. Otherwise start the server in this console
window (Ctrl+C stops it).
"""

import argparse
import os
import socket
import sys
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 50003


def candidate_hosts(host):
    """Addresses that may be serving PerfCattle on this machine."""
    hosts = [host]
    try:
        hosts.append(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    result = []
    for item in hosts:
        if item and item != "0.0.0.0" and item not in result:
            result.append(item)
    return result


def running_url(host, port):
    """Return the URL of a running PerfCattle instance, or None."""
    for addr in candidate_hosts(host):
        url = "http://{}:{}/".format(addr, port)
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                body = resp.read(200000)
                if resp.status == 200 and b"PerfCattle" in body:
                    return url
        except Exception:
            continue
    return None


def launch(host, port, debug=False, log_file=None):
    url = running_url(host, port)
    if url:
        print("[PerfCattle] Already running: {}".format(url))
        webbrowser.open(url)
        return 0

    from perfcattle.web import main

    main(host=host, port=port, debug=debug, log_file=log_file)
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Start PerfCattle.")
    parser.add_argument(
        "--host",
        default=os.environ.get("PERFCATTLE_HOST", "127.0.0.1"),
        help="listen address (default: 127.0.0.1; use 0.0.0.0 for LAN access)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PERFCATTLE_PORT", DEFAULT_PORT)),
        help="listen port (default: 50003)",
    )
    parser.add_argument("--debug", action="store_true", help="enable Flask debug mode")
    parser.add_argument("--log_file", default=None, help="runtime log path")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    return launch(args.host, args.port, args.debug, args.log_file)


if __name__ == "__main__":
    sys.exit(main())
