#!/usr/bin/env python3
"""Capture ESPHome API logs for a fixed duration with automatic reconnects.

This process only subscribes to ESPHome logs. It never opens or writes a UART.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path


STOP = False


def request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="makeskyblue-modbus-monitor.yaml")
    parser.add_argument("--device", default="makeskybluemodbusmonitor.local")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=24 * 60 * 60)
    parser.add_argument("--reconnect-delay", type=float, default=3.0)
    parser.add_argument("--allow-append", action="store_true")
    parser.add_argument("--firmware", type=Path)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    esphome = repo_root / ".venv" / "bin" / "esphome"
    if not esphome.exists():
        parser.error(f"ESPHome executable not found: {esphome}")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.output.exists() and args.output.stat().st_size > 0 and not args.allow_append:
        parser.error(
            f"refusing to mix capture sessions in existing file: {args.output}; "
            "use a new path or pass --allow-append explicitly"
        )

    config_path = (repo_root / args.config).resolve()
    firmware_path = args.firmware or (
        repo_root
        / ".esphome"
        / "build"
        / "makeskybluemodbusmonitor"
        / "build"
        / "firmware.ota.bin"
    )
    git_commit = git_value(repo_root, "rev-parse", "HEAD")
    git_dirty = bool(git_value(repo_root, "status", "--porcelain"))
    config_sha256 = sha256_file(config_path)
    firmware_sha256 = (
        sha256_file(firmware_path) if firmware_path.exists() else "missing"
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration
    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    with args.output.open("a", encoding="utf-8", buffering=1) as output:
        output.write(
            f"{timestamp()} CAPTURE_START device={args.device} "
            f"duration_seconds={args.duration} git_commit={git_commit} "
            f"git_dirty={str(git_dirty).lower()} "
            f"config_sha256={config_sha256} "
            f"firmware_sha256={firmware_sha256}\n"
        )

        while not STOP and time.monotonic() < deadline:
            output.write(f"{timestamp()} CONNECT_ATTEMPT device={args.device}\n")
            process = subprocess.Popen(
                [str(esphome), "logs", args.config, "--device", args.device],
                cwd=repo_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)

            try:
                while not STOP and time.monotonic() < deadline:
                    remaining = max(0.0, deadline - time.monotonic())
                    events = selector.select(timeout=min(2.0, remaining))
                    if not events:
                        if process.poll() is not None:
                            break
                        continue
                    line = process.stdout.readline()
                    if line:
                        output.write(f"{timestamp()} {line}")
                    elif process.poll() is not None:
                        break
            finally:
                selector.close()
                terminate(process)

            if not STOP and time.monotonic() < deadline:
                output.write(
                    f"{timestamp()} DISCONNECTED retry_seconds={args.reconnect_delay}\n"
                )
                time.sleep(min(args.reconnect_delay, deadline - time.monotonic()))

        reason = "signal" if STOP else "duration_complete"
        output.write(f"{timestamp()} CAPTURE_END reason={reason}\n")

    return 130 if STOP else 0


if __name__ == "__main__":
    sys.exit(main())
