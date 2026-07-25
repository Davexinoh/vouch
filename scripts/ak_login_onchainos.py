"""Use onchainos wallet AK login (omit email) with vouch/.env keys."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONCHAINOS = Path(r"C:\Users\Hp\onchainos\onchainos.exe")


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    env = load_env()
    # Common env names onchainos might read
    for src, dst in [
        ("OKX_API_KEY", "OKX_API_KEY"),
        ("OKX_SECRET_KEY", "OKX_SECRET_KEY"),
        ("OKX_PASSPHRASE", "OKX_PASSPHRASE"),
        ("OKX_API_KEY", "API_KEY"),
        ("OKX_SECRET_KEY", "SECRET_KEY"),
        ("OKX_PASSPHRASE", "PASSPHRASE"),
    ]:
        if env.get(src):
            env[dst] = env[src]
    print("keys present:", bool(env.get("OKX_API_KEY")), bool(env.get("OKX_SECRET_KEY")))
    r = subprocess.run(
        [str(ONCHAINOS), "wallet", "login", "--force"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=90,
    )
    print("stdout:", r.stdout[:2000])
    print("stderr:", r.stderr[:2000])
    print("code", r.returncode)
    # status after
    r2 = subprocess.run(
        [str(ONCHAINOS), "wallet", "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=30,
    )
    print("status:", r2.stdout[:1000])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
