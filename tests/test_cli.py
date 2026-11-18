"""The command line, including real runs of the server."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import ssl
import time
from pathlib import Path

import aiohttp
import pytest
from conftest import get, start_server, stop, wait_json

from htop_web import __version__
from htop_web.cli import build_parser, exposure_warning, host_specs, main, parse_auth


def test_help_and_version(capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["--help"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    assert "usage: htop-web" in out and "HTOP_WEB_AUTH" in out and "--no-verify-host" in out
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"htop-web {__version__}"


@pytest.mark.parametrize("argv", [
    ["--port", "0"], ["--port", "70000"], ["--interval", "0"], ["--procs", "0"],
    ["--procs", "501"], ["--ssh-port", "0"], ["--ssl-certfile", "cert.pem"], ["bad host"],
    ["@nouser"],
])
def test_bad_arguments(argv, capsys):
    with pytest.raises(SystemExit) as exit_:
        main(argv)
    assert exit_.value.code == 2


def test_bad_auth_env(monkeypatch):
    monkeypatch.setenv("HTOP_WEB_AUTH", "no-colon")
    with pytest.raises(SystemExit) as exit_:
        main([])
    assert exit_.value.code == 2


def test_defaults():
    args = build_parser().parse_args([])
    assert (args.port, args.bind, args.interval, args.procs, args.python) == (
        48110, "127.0.0.1", 2.0, 15, "python3")


def test_host_specs():
    assert [s.key for s in host_specs([], None, False)] == ["local"]
    specs = host_specs(["gpu01", "gpu01", "bob@db:2222", "local"], 2200, False)
    assert [s.key for s in specs] == ["gpu01", "bob@db:2222", "local"]
    assert specs[0].port == 2200 and specs[1].port == 2222
    assert [s.kind for s in host_specs(["ignored"], None, True)] == ["demo"] * 4


def test_parse_auth():
    assert parse_auth(None) is None and parse_auth("") is None
    assert parse_auth("admin:pa:ss") == ("admin", "pa:ss")
    for bad in ("admin", ":x", "admin:"):
        with pytest.raises(ValueError):
            parse_auth(bad)


def children_of(pid: int) -> list[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        return [int(x) for x in path.read_text().split()]
    except OSError:
        return []


@pytest.mark.skipif(not Path("/proc/stat").exists(), reason="needs Linux /proc")
def test_serves_this_machine_and_shuts_down_cleanly():
    proc, base = start_server("--interval", "0.5", "--procs", "7")
    try:
        data = wait_json(base)
        host = data["hosts"][0]
        assert host["key"] == "local" and host["kind"] == "local" and len(host["procs"]) == 7
        assert get(base + "/")[0] == 200
        status, text = get(base + "/htop.txt")
        assert status == 200 and "PID USER" in text
        collectors = children_of(proc.pid)
    finally:
        code = stop(proc)
    assert code == 0, proc.stderr.read()
    assert "htop-web" in proc.stdout.read()
    deadline = time.monotonic() + 5
    while any(Path(f"/proc/{pid}").exists() for pid in collectors) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(Path(f"/proc/{pid}").exists() for pid in collectors), "collector left behind"


def test_demo_with_basic_auth():
    proc, base = start_server("--demo", "--interval", "0.5",
                              env={"HTOP_WEB_AUTH": "viewer:correct horse"})
    try:
        data = wait_json(base, auth="viewer:correct horse")
        assert [h["key"] for h in data["hosts"]] == ["gpu-node-01", "web-01", "db-01", "build-01"]
        assert data["hosts"][0]["ncpu"] == 64
        assert get(base + "/htop.json")[0] == 401
        assert get(base + "/", auth="viewer:wrong")[0] == 401
    finally:
        assert stop(proc) == 0
    assert "correct horse" not in proc.stdout.read()


def test_warns_when_open_without_auth():
    assert exposure_warning("127.0.0.1", None) is None
    assert exposure_warning("0.0.0.0", ("a", "b")) is None
    assert "without authentication" in exposure_warning("0.0.0.0", None)
    assert "without authentication" in exposure_warning("192.0.2.7", None)


def self_signed(directory: Path) -> tuple[Path, Path]:
    """A throwaway certificate for 127.0.0.1, its own CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "htop-web test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    usage = dict.fromkeys(["content_commitment", "key_encipherment", "data_encipherment",
                           "key_agreement", "encipher_only", "decipher_only"], False)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                         crl_sign=True, **usage), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]),
                critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


async def test_https_with_certificate_files(tmp_path):
    cert, key = self_signed(tmp_path)
    proc, base = start_server("--demo", "--interval", "0.5", "--ssl-certfile", str(cert),
                              "--ssl-keyfile", str(key))
    https = base.replace("http://", "https://")
    trust = ssl.create_default_context(cafile=str(cert))
    try:
        async with aiohttp.ClientSession() as http:
            data = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    async with http.get(https + "/htop.json", ssl=trust) as resp:
                        data = await resp.json()
                    if data["hosts"] and data["hosts"][0]["status"] == "ok":
                        break
                except (aiohttp.ClientError, OSError):
                    pass
                await asyncio.sleep(0.2)
            assert data and data["hosts"][0]["status"] == "ok"
            async with http.ws_connect(https.replace("https://", "wss://") + "/ws",
                                       ssl=trust) as ws:
                assert (await ws.receive_json(timeout=5))["type"] == "hello"
                assert (await ws.receive_json(timeout=5))["type"] == "update"
            with pytest.raises((aiohttp.ClientError, OSError)):
                async with http.get(base + "/htop.json") as resp:  # plain HTTP: refused
                    await resp.read()
    finally:
        assert stop(proc) == 0
    assert f"{https}/" in proc.stdout.read()
