from typing import Any, Dict, List
import textwrap
import re
import math

from parser import Component


def _replace_default_token(s: str) -> str:
    s1 = re.sub(r"(?<!\\)\$default\b", "default", s)
    s1 = s1.replace(r"\$default", "$default")
    s2 = re.sub(
        r"(?<!\\)\$(\d+)\b",
        lambda m: f"__deref_index({m.group(1)}, default, args, kwargs)",
        s1,
    )
    s3 = re.sub(
        r"(?<!\\)\$([A-Za-z_]\w*)\b",
        lambda m: f"__deref('{m.group(1)}', default, args, kwargs)",
        s2,
    )
    s4 = s3.replace(r"\$", "$")
    s4 = s4.replace("&&", " and ").replace("||", " or ")
    return s4


def compile_namespace(components: Dict[str, Component]) -> Dict[str, Any]:
    import ast as _ast

    code_lines: List[str] = []

    code_lines.extend(
        [
            "def __invoke_child(func, value):",
            "    if func is exec:",
            "        if isinstance(value, str):",
            "            exec(value, globals())",
            "            return value",
            "        if isinstance(value, dict) and 'code' in value:",
            "            exec(value['code'], globals())",
            "            return value",
            "        return value",
            "",
            "    if isinstance(value, (list, tuple)):",
            "        return func(value)",
            "    if isinstance(value, dict):",
            "        name = getattr(func, '__name__', '')",
            "        if getattr(func, '__is_dsl_component', False) or name.startswith('__comp_'):",
            "            return func(value)",
            "        return func(**value)",
            "    return func(value)",
            "",
        ]
    )

    code_lines.extend(
        [
            "def __deref(name, default, args, kwargs):",
            "    if isinstance(default, dict) and name in default:",
            "        return default[name]",
            "    return kwargs.get(name)",
            "",
            "def __deref_index(i, default, args, kwargs):",
            "    i = int(i)",
            "    if isinstance(default, (list, tuple)) and len(default) > i:",
            "        return default[i]",
            "    if len(args) > i:",
            "        return args[i]",
            "    return None",
            "",
        ]
    )

    def _expr_for_value(v):
        if isinstance(v, str):
            if "$" in v:
                return _replace_default_token(v)
            return repr(v)
        if isinstance(v, dict):
            parts = [f"{repr(k)}: {_expr_for_value(val)}" for k, val in v.items()]
            return "{" + ", ".join(parts) + "}"
        if isinstance(v, (list, tuple)):
            parts = [_expr_for_value(x) for x in v]
            return "[" + ", ".join(parts) + "]"
        return repr(v)

    for name, comp in components.items():
        body_fn = f"__body_{name}"

        if comp.body_type == "expr":
            raw = comp.body
            try:
                lit = _ast.literal_eval(raw)
            except Exception:
                lit = None

            if isinstance(lit, dict):
                code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                code_lines.append(
                    "    d = dict(default) if isinstance(default, dict) else {}"
                )
                code_lines.append("    d.update({")
                for k, v in lit.items():
                    code_lines.append(f"        {repr(k)}: {_expr_for_value(v)},")
                code_lines.append("    })")
                code_lines.append("    return d")
                code_lines.append("")
            elif isinstance(lit, (list, tuple)):
                code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                code_lines.append(
                    f"    if isinstance(default, (list, tuple)): return list(default) + {_expr_for_value(lit)}"
                )
                code_lines.append(f"    return {_expr_for_value(lit)}")
                code_lines.append("")
            else:
                raw = comp.body
                if (raw.startswith("'") or raw.startswith('"')) and "$" in raw:
                    inner = _ast.literal_eval(raw)
                    expr = _replace_default_token(inner)
                else:
                    expr = _replace_default_token(comp.body)
                code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                code_lines.append(f"    return ({expr})")
                code_lines.append("")
        else:
            body = _replace_default_token(textwrap.dedent(comp.body))
            if re.search(r"^\s*def\s+__body\s*\(", body, flags=re.MULTILINE):
                body_replaced = re.sub(
                    r"^\s*def\s+__body\s*\(",
                    f"def {body_fn}(",
                    body,
                    count=1,
                    flags=re.MULTILINE,
                )
                code_lines.extend(body_replaced.splitlines())
                code_lines.append("")
            elif re.search(r"^\s*(def|class|import|from)\b", body, flags=re.MULTILINE):
                code_lines.extend(body.splitlines())
                code_lines.append("")
                code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                code_lines.append("    return default")
                code_lines.append("")
            else:
                first_nonblank = next(
                    (ln for ln in body.splitlines() if ln.strip()), ""
                )
                if first_nonblank.lstrip().startswith(
                    ("def ", "class ", "import ", "from ")
                ):
                    code_lines.extend(body.splitlines())
                    code_lines.append("")
                    code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                    code_lines.append("    return None")
                    code_lines.append("")
                else:
                    code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                    stripped_lines = [ln for ln in body.splitlines() if ln.strip()]
                    if len(stripped_lines) == 1 and not stripped_lines[
                        0
                    ].lstrip().startswith(
                        ("def ", "class ", "import ", "from ", "return ")
                    ):
                        single = stripped_lines[0]
                        code_lines.append(f"    return ({single})")
                    else:
                        if body.strip() == "":
                            code_lines.append("    pass")
                        else:
                            for line in body.splitlines():
                                if line.strip() == "":
                                    code_lines.append("")
                                else:
                                    code_lines.append(f"    {line.rstrip()}")
                    code_lines.append("")

        code_lines.append(f"def __comp_{name}(default=None, *args, **kwargs):")
        code_lines.append(f"    result = {body_fn}(default, *args, **kwargs)")
        for child in comp.children:
            code_lines.append(
                f"    _child_callable = globals().get('__comp_{child}', {child})"
            )
            code_lines.append(f"    result = __invoke_child(_child_callable, result)")
        code_lines.append("    return result")
        code_lines.append("")
        code_lines.append(f"{name} = __comp_{name}")
        code_lines.append(f"__comp_{name}.__is_dsl_component = True")
        code_lines.append("")

    code = "\n".join(code_lines)
    ns: Dict[str, Any] = {}
    exec(compile(code, "<dsl-compiled>", "exec"), ns)
    ns["_dsl_generated_code"] = code
    return ns


def run_component(ns: Dict[str, Any], name: str, default: Any = None):
    func = ns.get(name)
    if func is None:
        func = ns.get(f"__comp_{name}")
    if func is None:
        raise KeyError(f"Component {name!r} not found in the compiled namespace")
    return func(default)


def _deep_compare(
    a: Any, b: Any, float_tol: float = 1e-9, rel_tol: float = 1e-9
) -> bool:
    import math

    try:
        import numpy as np
    except Exception:
        np = None
    try:
        import pandas as pd
    except Exception:
        pd = None

    if a is b:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=float_tol)
    if type(a) != type(b):
        if np is not None and isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
            try:
                return np.array_equal(a, b) or np.allclose(
                    a, b, rtol=rel_tol, atol=float_tol
                )
            except Exception:
                return False
        return False
    if isinstance(a, float):
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=float_tol)
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_deep_compare(x, y, float_tol, rel_tol) for x, y in zip(a, b))
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_deep_compare(a[k], b[k], float_tol, rel_tol) for k in a.keys())
    if np is not None and isinstance(a, np.ndarray):
        try:
            return np.array_equal(a, b) or np.allclose(
                a, b, rtol=rel_tol, atol=float_tol
            )
        except Exception:
            return False
    if (
        pd is not None
        and isinstance(a, (pd.DataFrame, pd.Series))
        and isinstance(b, type(a))
    ):
        try:
            if isinstance(a, pd.DataFrame):
                pd.testing.assert_frame_equal(
                    a, b, check_exact=False, rtol=rel_tol, atol=float_tol
                )
            else:
                pd.testing.assert_series_equal(
                    a, b, check_exact=False, rtol=rel_tol, atol=float_tol
                )
            return True
        except AssertionError:
            return False
    try:
        return a == b
    except Exception:
        return False


def check(yaml_code: str, start: str = "main", expects: str = "result"):
    from parser import parse_components

    comps = parse_components(yaml_code)
    if expects not in comps:
        raise AssertionError(f"expects component {expects!r} not found in YAML")
    if start not in comps:
        keys = list(comps.keys())
        fallback = next((k for k in keys if k != expects), None)
        if fallback is None:
            raise AssertionError(
                f"no start component found in YAML (checked for {start!r})"
            )
        start = fallback

    ns = compile_namespace(comps)
    start_res = run_component(ns, start, default=None)
    expect_res = run_component(ns, expects, default=None)
    ok = _deep_compare(start_res, expect_res)
    if not ok:
        msg_lines = [
            f"YAML check failed: start={start!r} expects={expects!r}",
            f"start -> ({type(start_res).__name__}): {repr(start_res)}",
            f"expects-> ({type(expect_res).__name__}): {repr(expect_res)}",
            "",
            "---- generated python ----",
            ns.get("_dsl_generated_code", "<no code>"),
        ]
        raise AssertionError("\n".join(msg_lines))
