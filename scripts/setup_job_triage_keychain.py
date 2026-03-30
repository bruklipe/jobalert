#!/usr/bin/env python3
import getpass
import subprocess
from pathlib import Path


WORKSPACE = Path("/Users/ilvipeshku/Documents/Playground")
ENV_PATH = WORKSPACE / ".env"
DEFAULT_HOST = "imap.gmail.com"
DEFAULT_USER = "bruklipe@umich.edu"
DEFAULT_SERVICE = "indeed-job-triage-imap"


def prompt(default: str, label: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def upsert_env(path: Path, values: dict) -> None:
    existing = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.strip().startswith("#"):
                key, value = raw.split("=", 1)
                existing[key.strip()] = value.strip()
    existing.update(values)
    lines = [f"{key}={value}" for key, value in sorted(existing.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    print("Indeed job triage secure setup")
    host = prompt(DEFAULT_HOST, "IMAP host")
    user = prompt(DEFAULT_USER, "Email/username")
    service = prompt(DEFAULT_SERVICE, "Keychain service name")
    password = getpass.getpass("IMAP password (input hidden): ").strip()
    if not password:
        print("No password entered. Nothing changed.")
        return 1

    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", user, "-w", password],
        check=True,
    )
    upsert_env(
        ENV_PATH,
        {
            "INDEED_IMAP_HOST": host,
            "INDEED_IMAP_USER": user,
            "INDEED_IMAP_PASSWORD_KEYCHAIN_SERVICE": service,
        },
    )
    print(f"Wrote non-secret settings to {ENV_PATH}")
    print(f"Stored password in macOS Keychain service '{service}' for account '{user}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
