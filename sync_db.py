"""Download the latest database collected by GitHub Actions (branch `data`) into form4.db.

    python sync_db.py
    $env:SCAN_DISABLED = "1"; python app.py     # browse it with filters, editor etc., without scanning

Python writes the bytes itself: a PowerShell redirect (`git show ... > form4.db`) would
re-encode the binary file and corrupt it.
"""

import lzma
import subprocess
import sys
from pathlib import Path


def main() -> int:
    subprocess.run(["git", "fetch", "--depth=1", "origin", "data"], check=True)
    packed = subprocess.run(["git", "show", "origin/data:form4.db.xz"], capture_output=True)
    if packed.returncode == 0:  # current format: xz-compressed database
        Path("form4.db").write_bytes(lzma.decompress(packed.stdout))
        print(f"form4.db: {Path('form4.db').stat().st_size / 1e6:.1f} MB (skompresowana {len(packed.stdout) / 1e6:.1f} MB)")
    for name in (("scan_status.json",) if packed.returncode == 0 else ("form4.db", "scan_status.json")):
        out = subprocess.run(["git", "show", f"origin/data:{name}"], capture_output=True)
        if out.returncode == 0:
            Path(name).write_bytes(out.stdout)
            print(f"{name}: {len(out.stdout) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
