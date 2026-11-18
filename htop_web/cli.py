"""Command line entry point: ``htop-web [options] [HOST ...]``."""

from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys

from aiohttp import web

from . import __version__
from .app import Options, create_app, is_loopback
from .demo import demo_specs
from .model import HostSpec, parse_host

DEFAULT_PORT = 48110
AUTH_ENV = "HTOP_WEB_AUTH"

EPILOG = f"""\
HOST is [USER@]HOST[:PORT], an alias from ~/.ssh/config, or 'local' for this
machine without SSH. With no HOST, htop-web shows this machine.

Hosts need Linux and python3 (3.6+); nothing else is installed on them.

Authentication: set {AUTH_ENV}=user:password in the environment to require
HTTP Basic auth (it is read from the environment so it never shows up in a
process list). Use it with --ssl-certfile/--ssl-keyfile beyond localhost.

examples:
  htop-web
  htop-web gpu01 gpu02 alice@db01:2222
  htop-web --bind 0.0.0.0 --port 8080 local web01
  curl -s localhost:{DEFAULT_PORT}/htop.ansi
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="htop-web", description="A web interface of htop: watch many Linux hosts "
        "in one page, over SSH, with nothing to install on them.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hosts", nargs="*", metavar="HOST", help="hosts to watch")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port of the web server (default: {DEFAULT_PORT})")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="address to listen on (default: 127.0.0.1; "
                             "0.0.0.0 lets other machines connect)")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between updates (default: 2)")
    parser.add_argument("--procs", type=int, default=15,
                        help="process rows per host; the page can change it (default: 15)")
    parser.add_argument("--ssh-port", type=int, default=None,
                        help="SSH port for hosts that give none (default: ~/.ssh/config, "
                             "else 22)")
    parser.add_argument("--no-verify-host", action="store_true",
                        help="skip SSH host key checks (not recommended)")
    parser.add_argument("--python", default="python3",
                        help="Python interpreter on the hosts (default: python3)")
    parser.add_argument("--ssl-certfile", help="certificate file, to serve HTTPS")
    parser.add_argument("--ssl-keyfile", help="private key file, to serve HTTPS")
    parser.add_argument("--demo", action="store_true",
                        help="show made-up hosts instead (to try it out, or for screenshots)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log more")
    parser.add_argument("--version", action="version", version=f"htop-web {__version__}")
    return parser


def parse_auth(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    user, sep, password = value.partition(":")
    if not sep or not user or not password:
        raise ValueError(f"{AUTH_ENV} must look like user:password")
    return user, password


def exposure_warning(bind: str, auth: tuple[str, str] | None) -> str | None:
    """A warning when anyone on the network could read the process lists."""
    if is_loopback(bind) or auth is not None:
        return None
    return (f"warning: listening beyond localhost without authentication. Anyone who can "
            f"reach this port sees every process and command line. Set "
            f"{AUTH_ENV}=user:password to require a login.")


def host_specs(names: list[str], ssh_port: int | None, demo: bool) -> list[HostSpec]:
    if demo:
        return demo_specs()
    specs: list[HostSpec] = []
    for name in names or ["local"]:
        spec = parse_host(name, ssh_port)
        if spec.key not in {s.key for s in specs}:
            specs.append(spec)
    return specs


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 < args.port < 65536:
        parser.error("--port must be between 1 and 65535")
    if not 0.2 <= args.interval <= 3600:
        parser.error("--interval must be between 0.2 and 3600 seconds")
    if not 1 <= args.procs <= 500:
        parser.error("--procs must be between 1 and 500")
    if args.ssh_port is not None and not 0 < args.ssh_port < 65536:
        parser.error("--ssh-port must be between 1 and 65535")
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("--ssl-certfile and --ssl-keyfile go together")
    try:
        specs = host_specs(args.hosts, args.ssh_port, args.demo)
        auth = parse_auth(os.environ.get(AUTH_ENV))
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    ssl_context = None
    if args.ssl_certfile:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(args.ssl_certfile, args.ssl_keyfile)

    options = Options(interval=args.interval, procs=args.procs, python=args.python,
                      verify_host=not args.no_verify_host, auth=auth, bind=args.bind)
    app = create_app(specs, options)

    scheme = "https" if ssl_context else "http"
    shown = f"[{args.bind}]" if ":" in args.bind else args.bind
    print(f"htop-web {__version__} on {scheme}://{shown}:{args.port}/ watching "
          f"{', '.join(s.key for s in specs)}", flush=True)
    warning = exposure_warning(args.bind, auth)
    if warning:
        print(warning, file=sys.stderr, flush=True)
    web.run_app(app, host=args.bind, port=args.port, ssl_context=ssl_context, print=None,
                access_log=logging.getLogger("aiohttp.access") if args.verbose else None)
    return 0
