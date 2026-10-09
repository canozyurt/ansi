#!/usr/bin/env python3
"""Print a bcrypt hash of the password read from stdin.

Reuses the hash stored in the file given as the first argument when it still matches the
password, so the security configuration only changes when a password changes.
"""
import sys

import bcrypt

password = sys.stdin.read().rstrip("\n").encode()
current = b""
if len(sys.argv) > 1:
    try:
        with open(sys.argv[1], "rb") as handle:
            current = handle.read().strip()
    except FileNotFoundError:
        pass
if current:
    try:
        if bcrypt.checkpw(password, current):
            print(current.decode())
            sys.exit(0)
    except ValueError:
        pass
print(bcrypt.hashpw(password, bcrypt.gensalt(rounds=12)).decode())
