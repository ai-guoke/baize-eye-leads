# -*- coding: utf-8 -*-
import subprocess, sys
from pathlib import Path
ROOT = Path(r"F:\企查查大数据\线索池平台")
log = ROOT / "output" / "repair_muni_geo.log"
py = sys.executable
cmd = [py, "-u", str(ROOT / "etl" / "repair_muni_geo.py")]
with log.open("w", encoding="utf-8") as f:
    f.write("launch " + " ".join(cmd) + "\n")
    f.flush()
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    Path(ROOT / "output" / "repair_muni_geo.pid").write_text(str(p.pid), encoding="utf-8")
    sys.exit(p.wait())
