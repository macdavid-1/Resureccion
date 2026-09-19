"""CLI: generate OWNER_PASSWORD_HASH for the single owner account.

Usage:
    python -m app.scripts.set_password            # interactive prompt
    python -m app.scripts.set_password "mypass"   # inline (careful: shell history)
"""
from __future__ import annotations

import getpass
import sys

from app.security import hash_password


def main() -> int:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = getpass.getpass("Owner password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if not password:
        print("Password must be non-empty.", file=sys.stderr)
        return 1
    print(f"OWNER_PASSWORD_HASH={hash_password(password)}")
    print("Set this (and a long random AUTH_SECRET) in the deployment environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
