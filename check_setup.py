"""Check your configuration WITHOUT printing any secret values.

Usage (from the project folder, virtual environment active):
    python check_setup.py            # checks variables and the database
    python check_setup.py --daraja   # also tests Daraja Sandbox login
"""

import sys

import db
import server

OPTIONAL = ("DATABASE_URL", "APP_ENV", "MAX_AMOUNT", "CALLBACK_ALLOWED_IPS", "TRUSTED_PROXY_HOPS")


def main():
    ok = True
    print("Required variables:")
    for name in server.REQUIRED_ENV:
        present = bool(server._env(name))
        ok &= present
        print(f"  {name:24} {'SET' if present else 'MISSING'}")
    print("Optional variables:")
    for name in OPTIONAL:
        print(f"  {name:24} {'SET' if server._env(name) else 'not set'}")

    problems = server.config_problems()
    print("\nConfiguration problems:", "none" if not problems else "")
    for p in problems:
        print("  -", p)
    ok &= not problems

    print("\nDatabase:", "reachable" if db.ping() else "NOT reachable")
    ok &= db.ping()

    if "--daraja" in sys.argv:
        try:
            server.get_access_token(force=True)
            print("Daraja Sandbox login: OK")
        except server.DarajaError as exc:
            ok = False
            print(f"Daraja Sandbox login: FAILED ({exc})")

    print("\nRESULT:", "all good" if ok else "fix the items above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
