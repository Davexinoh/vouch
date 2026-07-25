from pathlib import Path
import re
import subprocess

subprocess.run(
    [r"C:\Users\Hp\onchainos\onchainos.exe", "agent", "get-agents", "--agent-ids", "5127"],
    capture_output=True,
)
root = Path.home() / ".onchainos"
for p in root.rglob("*"):
    if not p.is_file() or p.stat().st_size > 5_000_000:
        continue
    try:
        t = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for m in re.finditer(
        r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", t
    ):
        print("JWT in", p, "len", len(m.group(0)))
    for m in re.finditer(r"accessToken[\"']?\s*[:=]\s*[\"']([^\"']{20,})", t):
        print("accessToken in", p, "len", len(m.group(1)), "prefix", m.group(1)[:12])
print("done")
