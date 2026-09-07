# -*- coding: utf-8 -*-
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

print("python     ", sys.version.split()[0])
print("cpu_count  ", os.cpu_count())
for name in ("numpy", "pyarrow", "duckdb", "pandas", "openpyxl"):
    try:
        mod = __import__(name)
        print(f"{name:<11}", getattr(mod, "__version__", "?"))
    except ImportError:
        print(f"{name:<11}", "NOT INSTALLED")
