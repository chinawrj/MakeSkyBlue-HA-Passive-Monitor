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
DEFAULT_STALE_OUTPUT_SECONDS = 120.0
DEFAULT_HEARTBEAT_SECONDS = 30.0


def request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def timestamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def stale_output_due(
    last_output_monotonic: float, now_monotonic: float, limit_seconds: float
) -> bool:
    """Return whether an apparently live log subscription needs reconnecting."""
    return now_monotonic - last_output_monotonic >= limit_seconds


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
    parser.add_argument(
        "--stale-output-seconds",
        type=float,
        default=DEFAULT_STALE_OUTPUT_SECONDS,
        help=(
            "restart an esphome logs child that stays alive without producing "
            "any output for this long"
        ),
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_SECONDS,
        help="write a local capture-process heartbeat at this interval",
    )
    parser.add_argument("--allow-append", action="store_true")
    parser.add_argument("--firmware", type=Path)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    esphome = repo_root / ".venv" / "bin" / "esphome"
    if not esphome.exists():
        parser.error(f"ESPHome executable not found: {esphome}")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.stale_output_seconds <= 0:
        parser.error("--stale-output-seconds must be positive")
    if args.heartbeat_seconds <= 0:
        parser.error("--heartbeat-seconds must be positive")
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
            f"firmware_sha256={firmware_sha256} "
            f"stale_output_seconds={args.stale_output_seconds:g} "
            f"heartbeat_seconds={args.heartbeat_seconds:g}\n"
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
            last_child_output = time.monotonic()
            next_heartbeat = last_child_output + args.heartbeat_seconds
            disconnect_reason = "process_exit"

            try:
                while not STOP and time.monotonic() < deadline:
                    now = time.monotonic()
                    remaining = max(0.0, deadline - time.monotonic())
                    until_heartbeat = max(0.0, next_heartbeat - now)
                    until_stale = max(
                        0.0,
                        args.stale_output_seconds - (now - last_child_output),
                    )
                    events = selector.select(
                        timeout=min(2.0, remaining, until_heartbeat, until_stale)
                    )
                    if events:
                        line = process.stdout.readline()
                        if line:
                            last_child_output = time.monotonic()
                            output.write(f"{timestamp()} {line}")
                        elif process.poll() is not None:
                            disconnect_reason = "process_exit"
                            break

                    now = time.monotonic()
                    if now >= next_heartbeat:
                        output.write(
                            f"{timestamp()} CAPTURE_HEARTBEAT "
                            f"child_pid={process.pid} "
                            f"child_alive={str(process.poll() is None).lower()} "
                            f"seconds_since_output={now - last_child_output:.3f}\n"
                        )
                        next_heartbeat = now + args.heartbeat_seconds

                    if stale_output_due(
                        last_child_output, now, args.stale_output_seconds
                    ):
                        output.write(
                            f"{timestamp()} STALE_OUTPUT child_pid={process.pid} "
                            f"seconds_since_output={now - last_child_output:.3f} "
                            "action=reconnect\n"
                        )
                        disconnect_reason = "stale_output"
                        break

                    if not events:
                        if process.poll() is not None:
                            disconnect_reason = "process_exit"
                            break
                        continue
            finally:
                selector.close()
                terminate(process)

            if not STOP and time.monotonic() < deadline:
                output.write(
                    f"{timestamp()} DISCONNECTED reason={disconnect_reason} "
                    f"returncode={process.returncode} "
                    f"retry_seconds={args.reconnect_delay}\n"
                )
                time.sleep(min(args.reconnect_delay, deadline - time.monotonic()))

        reason = "signal" if STOP else "duration_complete"
        output.write(f"{timestamp()} CAPTURE_END reason={reason}\n")

    return 130 if STOP else 0


if __name__ == "__main__":
    sys.exit(main())
