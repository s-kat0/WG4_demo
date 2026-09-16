"""Create an Argon2id hash without placing plaintext in shell history."""

from __future__ import annotations

from getpass import getpass
from hmac import compare_digest

from argon2 import PasswordHasher


def main() -> int:
    first = getpass("Password: ")
    second = getpass("Password again: ")
    if not first:
        print("Password must not be empty.")
        return 1
    if not compare_digest(first, second):
        print("Passwords do not match.")
        return 1
    hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
    print(hasher.hash(first))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
