import os
import inspect
import sys

# Ensure src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from parser import parse_components
from runtime import compile_namespace, run_component, check


def test_compile_and_run_basic_fixture():
    # Simple YAML: a main component that returns 1 and expects component that returns 1
    yaml = """
main:
  1
expects:
  1
"""
    comps = parse_components(yaml)
    ns = compile_namespace(comps)
    assert "main" in ns
    assert "expects" in ns
    assert run_component(ns, "main") == run_component(ns, "expects")


def test_py_helper_exec_expr():
    # Ensure the py helper can evaluate expressions and exec statements
    yaml = """
main:
  |
    import math
    x = 2
    x + math.sqrt(9)
expects:
  5.0
"""
    # Use the existing check harness which should use compile_namespace
    check(yaml, start="main", expects="expects")
