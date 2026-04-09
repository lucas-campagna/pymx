#!/usr/bin/env python3
"""
tests/test_yaml_runner.py

Discover all YAML tests under this tests/ directory and run each one.

Each YAML file must define components at the top level. By convention each
YAML test should include two components:

main:   # the component under test (start)
result: # the expected-result component (expects)

The test runner will compile the DSL into in-memory Python functions, run
main() and result(), and assert the results are equal.

Usage:
    pip install pyyaml pytest
    pytest -q tests/test_yaml_runner.py
"""

from pathlib import Path
import re
import textwrap
import math
from typing import Any, Dict, List

import sys
from pathlib import Path as _P

# Add project src/ to sys.path so tests can import it as a module
_ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
from parser import parse_components, Component
import yaml
import pytest

# optional NA-friendly comparators
try:
    import numpy as np
except Exception:
    np = None

try:
    import pandas as pd
except Exception:
    pd = None


from runtime import check
# if equal, the function returns None (pytest perceives it as success)


# Discover YAML files in this tests/ directory (this file is intended to live in tests/)
BASE = Path(__file__).parent
YAML_FILES = sorted({p for ext in ("*.yml", "*.yaml") for p in BASE.rglob(ext)})


# Parametrize one pytest case per YAML file found
@pytest.mark.parametrize("yaml_path", YAML_FILES, ids=[str(p) for p in YAML_FILES])
def test_yaml_file(yaml_path: Path):
    yaml_text = yaml_path.read_text()
    try:
        check(yaml_text, start="main", expects="result")
    except AssertionError as e:
        # re-raise with filename context for clearer pytest output
        raise AssertionError(f"YAML test failed for {yaml_path}:\n{e}")


if __name__ == "__main__":
    # manual runner for quick verification (not used by pytest)
    if not YAML_FILES:
        print("No YAML files found under", BASE)
    else:
        for p in YAML_FILES:
            print("Running", p)
            try:
                check(p.read_text(), start="main", expects="result")
            except AssertionError as e:
                print("FAILED:", p)
                print(e)
                raise SystemExit(1)
        print("All YAML tests passed.")
