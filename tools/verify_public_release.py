#!/usr/bin/env python3
"""Fail closed when a public release contains files or credentials outside scope."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ALLOWED_TRACKED_FILES = {
    ".github/workflows/ci.yml",
    ".gitignore",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE.md",
    "README.md",
    "components/passive_modbus_monitor.h",
    "components/direct_mode_preview.h",
    "components/uart_capture_ring.h",
    "docs/atoms3-g1-g2-modbus-monitor.md",
    "docs/direct-communication-module-transition.md",
    "docs/reproduce-from-scratch.md",
    "docs/wifi-module-startup-sequence.md",
    "home-assistant/makeskyblue_uart_capture.yaml",
    "makeskyblue-modbus-monitor.yaml",
    "makeskyblue-local-link.yaml",
    "packages/makeskyblue-passive-entities.yaml",
    "registers/makeskyblue-observed-registers.csv",
    "requirements.txt",
    "secrets.example.yaml",
    "tests/passive_modbus_monitor_test.cpp",
    "tests/direct_mode_preview_test.cpp",
    "tests/ci/secrets.yaml",
    "tests/test_capture_tools.py",
    "tests/uart_capture_ring_test.cpp",
    "tools/analyze_uart_capture.py",
    "tools/capture_esphome_logs.py",
    "tools/generate_makeskyblue_passive.py",
    "tools/verify_public_release.py",
    "wifi.example.yaml",
    "wireguard.example.yaml",
}

FORBIDDEN_PATH_PARTS = {
    "captures",
    "firmware-backups",
    "references",
    "reports",
    "tmp",
    "__pycache__",
}

ABSOLUTE_USER_PATH = re.compile(
    r"(?:/" r"(?:Users|home)/[^/\s]+/|/" r"root/|"
    r"[A-Za-z]:[\\/]" r"Users[\\/][^\\/\s]+[\\/])"
)
INLINE_SECRET = re.compile(
    r"(?i)^\s*(?:key|[A-Za-z0-9_]*(?:private_key|preshared_key|"
    r"encryption_key|password|secret|token))\s*:\s*"
    r"(?![!][ ]?(?:secret|env_var)\b|[\"']?replace(?:-|_))([^#\s].*)$"
)
WIREGUARD_CONF_SECRET = re.compile(r"(?i)^\s*(?:PrivateKey|PresharedKey)\s*=")
BASE64_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{42,44}={0,2}(?![A-Za-z0-9+/=])"
)


def tracked_files(root: Path) -> set[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root, text=False
    )
    return {item.decode() for item in output.split(b"\0") if item}


def verify(root: Path) -> list[str]:
    errors: list[str] = []
    tracked = tracked_files(root)
    missing = ALLOWED_TRACKED_FILES - tracked
    unexpected = tracked - ALLOWED_TRACKED_FILES
    if missing:
        errors.append("missing tracked files: " + ", ".join(sorted(missing)))
    if unexpected:
        errors.append("unexpected tracked files: " + ", ".join(sorted(unexpected)))

    for relative in sorted(tracked & ALLOWED_TRACKED_FILES):
        path = root / relative
        if path.is_symlink():
            errors.append(f"symlink is not permitted: {relative}")
            continue
        if any(part in FORBIDDEN_PATH_PARTS for part in Path(relative).parts):
            errors.append(f"forbidden path in release: {relative}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"binary file is not permitted: {relative}")
            continue
        if "\r" in content:
            errors.append(f"non-LF line ending: {relative}")
        for number, line in enumerate(content.splitlines(), 1):
            if line.endswith((" ", "\t")):
                errors.append(f"trailing whitespace: {relative}:{number}")
            if ABSOLUTE_USER_PATH.search(line):
                errors.append(f"absolute user path: {relative}:{number}")
            if INLINE_SECRET.search(line):
                errors.append(f"possible inline credential: {relative}:{number}")
            if WIREGUARD_CONF_SECRET.search(line):
                errors.append(f"WireGuard credential syntax: {relative}:{number}")
            if BASE64_CREDENTIAL.search(line):
                errors.append(f"possible base64 credential: {relative}:{number}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    errors = verify(root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"public release boundary: PASS ({len(ALLOWED_TRACKED_FILES)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
