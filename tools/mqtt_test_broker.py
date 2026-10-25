"""Dev-only local MQTT test broker (amqtt), for exercising Real Data mode without hardware.

Runs in its own venv so amqtt's pinned `websockets` version cannot change the app's venv:

    python3 -m venv .venv-mqtt && .venv-mqtt/bin/pip install amqtt==0.12.1
    .venv-mqtt/bin/python tools/mqtt_test_broker.py --port 1883
    .venv-mqtt/bin/python tools/mqtt_test_broker.py --port 1883 --user udyam      # password from UDYAM_MQTT_TEST_PASS

Then start the app with UDYAM_MQTT_HOST=127.0.0.1 (and UDYAM_MQTT_PORT / UDYAM_MQTT_USER / UDYAM_MQTT_PASS).

Safety: it listens on 127.0.0.1 only and refuses any other address. It is a test fixture, not a production broker:
no TLS, no persistence. The password is never printed or logged; the hash file lives in a temporary folder that is
removed on exit. It prints "READY <host>:<port>" once it accepts connections.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path

from amqtt.broker import Broker

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def build_config(host: str, port: int, password_file: Path | None) -> dict:
    plugins: dict = {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": password_file is None}}
    if password_file is not None:
        plugins["amqtt.plugins.authentication.FileAuthPlugin"] = {"password_file": str(password_file)}
    return {"listeners": {"default": {"type": "tcp", "bind": f"{host}:{port}"}}, "plugins": plugins}


async def serve(host: str, port: int, user: str | None, password: str | None) -> None:
    with tempfile.TemporaryDirectory(prefix="udyam-mqtt-") as tmp:
        pw_file = None
        if user:
            from pwdlib import PasswordHash
            from pwdlib.hashers.argon2 import Argon2Hasher
            pw_file = Path(tmp) / "passwd"
            pw_file.write_text(f"{user}:{PasswordHash((Argon2Hasher(),)).hash(password)}\n")
            pw_file.chmod(0o600)
        broker = Broker(build_config(host, port, pw_file))
        await broker.start()
        print(f"READY {host}:{port}" + (" (password required)" if user else " (anonymous)"), flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        try:
            await asyncio.wait_for(broker.shutdown(), timeout=5)  # do not hang on clients that are still connected
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - exiting anyway
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user", default=None, help="require this username; password from UDYAM_MQTT_TEST_PASS")
    args = ap.parse_args()
    if args.host not in LOOPBACK:
        sys.exit("refusing to listen on a non-loopback address: this broker is a local test fixture only")
    password = os.environ.get("UDYAM_MQTT_TEST_PASS") if args.user else None
    if args.user and not password:
        sys.exit("--user needs the password in UDYAM_MQTT_TEST_PASS (never on the command line)")
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(serve(args.host, args.port, args.user, password))


if __name__ == "__main__":
    main()
