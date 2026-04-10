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

    # Local helper used at compile-time to normalize Python source produced
    # from YAML block bodies. It mirrors the runtime __dsl_normalize_script
    # but runs here so we can embed normalized source into generated code
    # (fixes patterns like `value.join(', ')` -> `', '.join(value)`).
    def _normalize_source(src: str) -> str | None:
        try:
            tree = _ast.parse(src, mode="exec")

            class _Norm(_ast.NodeTransformer):
                def visit_Call(self, node: _ast.Call) -> _ast.AST:
                    # Only perform the reversed-join transformation at compile-time.
                    # Avoid altering Dict/List/Tuple structures here to prevent
                    # accidental conversion of variable names (eg. for-loop
                    # targets) into string constants.
                    node = self.generic_visit(node)
                    try:
                        if (
                            isinstance(node.func, _ast.Attribute)
                            and node.func.attr == "join"
                            and len(node.args) == 1
                        ):
                            sep_arg = node.args[0]
                            if isinstance(sep_arg, _ast.Constant) and isinstance(
                                sep_arg.value, str
                            ):
                                # transform X.join(sep) -> sep.join(X)
                                new_func = _ast.Attribute(
                                    value=_ast.Constant(value=sep_arg.value),
                                    attr="join",
                                    ctx=_ast.Load(),
                                )
                                new_node = _ast.Call(
                                    func=new_func, args=[node.func.value], keywords=[]
                                )
                                return _ast.copy_location(new_node, node)
                        # If a block includes exec(<code>) without explicit globals,
                        # ensure the exec runs in module globals so imports and
                        # definitions persist across components. Rewrite
                        # exec(x) -> exec(x, globals()) when only one arg is present.
                        if (
                            isinstance(node.func, _ast.Name)
                            and node.func.id == "exec"
                            and len(node.args) == 1
                        ):
                            globals_call = _ast.Call(
                                func=_ast.Name(id="globals", ctx=_ast.Load()),
                                args=[],
                                keywords=[],
                            )
                            new_args = [node.args[0], globals_call]
                            new_node = _ast.Call(
                                func=node.func, args=new_args, keywords=node.keywords
                            )
                            return _ast.copy_location(new_node, node)
                    except Exception:
                        pass
                    return node

            tree = _Norm().visit(tree)
            _ast.fix_missing_locations(tree)
            try:
                return _ast.unparse(tree)
            except Exception:
                return None
        except Exception:
            return None

    # Helper: normalize DSL-style literal scripts so that bare identifiers
    # used inside dict/list literals become Python string constants. This
    # allows exec() to accept DSL-notation like `{math: [cos, pi]}` by
    # turning it into `{'math': ['cos', 'pi']}` before execution.
    code_lines.extend(
        [
            "def __dsl_normalize_script(s):",
            "    try:",
            "        import ast",
            "        tree = ast.parse(s, mode='exec')",
            "        class _Norm(ast.NodeTransformer):",
            "            def visit_Dict(self, node):",
            "                # convert bare-name dict keys into string constants",
            "                new_keys = []",
            "                for k in node.keys:",
            "                    if isinstance(k, ast.Name):",
            "                        new_keys.append(ast.Constant(value=k.id))",
            "                    else:",
            "                        new_keys.append(self.visit(k))",
            "                node.keys = new_keys",
            "                node.values = [self.visit(v) for v in node.values]",
            "                return node",
            "            def visit_List(self, node):",
            "                node.elts = [ast.Constant(value=el.id) if isinstance(el, ast.Name) else self.visit(el) for el in node.elts]",
            "                return node",
            "            def visit_Tuple(self, node):",
            "                node.elts = [ast.Constant(value=el.id) if isinstance(el, ast.Name) else self.visit(el) for el in node.elts]",
            "                return node",
            "            def visit_Call(self, node):",
            "                # Transform value.join(', ') -> ', '.join(value) to support",
            "                # code that uses the reversed join pattern inside f-strings",
            "                node = self.generic_visit(node)",
            "                try:",
            "                    if isinstance(node.func, ast.Attribute) and node.func.attr == 'join':",
            "                        receiver = node.func.value",
            "                        if isinstance(receiver, ast.Name) and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):",
            "                            sep = node.args[0]",
            "                            new_func = ast.Attribute(value=ast.Constant(value=sep.value), attr='join', ctx=ast.Load())",
            "                            new_node = ast.Call(func=new_func, args=[receiver], keywords=[])",
            "                            return ast.copy_location(new_node, node)",
            "                except Exception:",
            "                    pass",
            "                return node",
            "        tree = _Norm().visit(tree)",
            "        ast.fix_missing_locations(tree)",
            "        try:",
            "            src = ast.unparse(tree)",
            "            return src",
            "        except Exception:",
            "            return compile(tree, '<dsl-norm>', 'exec')",
            "    except Exception:",
            "        return None",
            "",
            "def py(value):",
            "    # Simple script runner: exec all lines except the last, eval the last.",
            "    # Accepts only a raw string value; other types are returned unchanged.",
            "    if not isinstance(value, str):",
            "        return value",
            "    lines = value.splitlines()",
            "    # drop trailing blank lines so final eval is meaningful",
            "    while lines and not lines[-1].strip():",
            "        lines.pop()",
            "    if not lines:",
            "        return None",
            "    if len(lines) == 1:",
            "        return eval(lines[0], globals())",
            '    head = "\\n".join(lines[:-1])',
            "    tail = lines[-1]",
            "    if head:",
            "        exec(head, globals())",
            "    return eval(tail, globals())",
            "",
            "def __invoke_child(func, value):",
            "    # Treat calls to exec/eval as 'run a python script' and route them",
            "    # through the py helper which handles eval/exec and returns values.",
            "    if func is exec or func is eval:",
            "        # When a string is provided, prefer evaluating it as an expression",
            "        # (so simple expressions return their value) and otherwise try to",
            "        # execute it. If execution fails due to DSL-style bare identifiers",
            "        # inside literals, attempt a normalized AST-based transform and exec",
            "        # that instead.",
            "        if isinstance(value, str):",
            "            # If the string looks like a DSL-style component call",
            "            # (eg. comp_a(abc=1, def=2)) parse it and invoke the compiled",
            "            # component directly. This avoids Python syntax errors when",
            "            # keyword names collide with Python keywords (like 'def').",
            "            import ast as _ast_call, re as _rcp",
            '            mcall = _rcp.match(r"^\\s*([A-Za-z_]\\w*)\\s*\\((.*)\\)\\s*$", value, flags=_rcp.S)',
            "            if mcall:",
            "                cname = mcall.group(1)",
            "                argtext = mcall.group(2)",
            "                kw = {}",
            "                pos = []",
            "                if argtext.strip() != '':",
            "                    try:",
            "                        # Parse the arguments via AST to handle commas in strings",
            "                        call_src = 'f(' + argtext + ')'",
            "                        expr = _ast_call.parse(call_src, mode='eval').body",
            "                        for a in expr.args:",
            "                            try:",
            "                                val = _ast_call.literal_eval(a)",
            "                            except Exception:",
            "                                try:",
            "                                    # Evaluate complex expressions in the current globals",
            "                                    val = eval(compile(_ast_call.Expression(a), '<ast>', 'eval'), globals())",
            "                                except Exception:",
            "                                    try:",
            "                                        val = _ast_call.unparse(a)",
            "                                    except Exception:",
            "                                        val = None",
            "                            pos.append(val)",
            "                        for k in expr.keywords:",
            "                            if k.arg is None:",
            "                                try:",
            "                                    v = eval(compile(_ast_call.Expression(k.value), '<ast>', 'eval'), globals())",
            "                                except Exception:",
            "                                    try:",
            "                                        v = _ast_call.literal_eval(k.value)",
            "                                    except Exception:",
            "                                        v = None",
            "                                if isinstance(v, dict):",
            "                                    kw.update(v)",
            "                            else:",
            "                                key = k.arg",
            "                                try:",
            "                                    val = _ast_call.literal_eval(k.value)",
            "                                except Exception:",
            "                                    try:",
            "                                        val = eval(compile(_ast_call.Expression(k.value), '<ast>', 'eval'), globals())",
            "                                    except Exception:",
            "                                        try:",
            "                                            val = _ast_call.unparse(k.value)",
            "                                        except Exception:",
            "                                            val = None",
            "                                kw[key] = val",
            "                    except Exception:",
            "                        # Fallback to the previous simple top-level comma splitter",
            "                        parts = []",
            "                        cur = ''",
            "                        depth = 0",
            "                        for ch in argtext:",
            "                            if ch in '([{':",
            "                                depth += 1",
            "                            elif ch in ')]}':",
            "                                depth -= 1",
            "                            if ch == ',' and depth == 0:",
            "                                parts.append(cur)",
            "                                cur = ''",
            "                            else:",
            "                                cur += ch",
            "                        if cur.strip():",
            "                            parts.append(cur)",
            "                        for p in parts:",
            "                            s = p.strip()",
            "                            if s == '':",
            "                                continue",
            "                            if '=' in s:",
            "                                k,v = s.split('=',1)",
            "                                key = k.strip()",
            "                                vs = v.strip()",
            "                                try:",
            "                                    import ast as _ast_lit",
            "                                    val = _ast_lit.literal_eval(vs)",
            "                                except Exception:",
            "                                    try:",
            "                                        val = eval(vs, globals())",
            "                                    except Exception:",
            "                                        val = vs",
            "                                kw[key] = val",
            "                            else:",
            "                                try:",
            "                                    import ast as _ast_lit",
            "                                    vv = _ast_lit.literal_eval(s)",
            "                                except Exception:",
            "                                    try:",
            "                                        vv = eval(s, globals())",
            "                                    except Exception:",
            "                                        vv = s",
            "                                pos.append(vv)",
            "                target = globals().get(cname) or globals().get('__comp_' + cname)",
            "                if target is not None:",
            "                    if kw:",
            "                        return target(kw)",
            "                    if len(pos) == 1:",
            "                        return target(pos[0])",
            "                    if pos:",
            "                        return target(pos)",
            "                    return target(None)",
            "            # Run the provided string through the py helper which will",
            "            # attempt eval(), exec(), and evaluate the last expression.",
            "            before = set(globals().keys())",
            "            try:",
            "                rv = py(value)",
            "                if rv is not None:",
            "                    return rv",
            "            except Exception:",
            "                pass",
            "            after = set(globals().keys())",
            "            created = [n for n in after - before if callable(globals().get(n))]",
            "            if len(created) == 1:",
            "                return globals()[created[0]]",
            "            created_noncall = [n for n in after - before if not callable(globals().get(n))]",
            "            if len(created_noncall) == 1:",
            "                return globals()[created_noncall[0]]",
            "            return value",
            "        if isinstance(value, dict) and 'code' in value:",
            "            before = set(globals().keys())",
            "            try:",
            "                rv = py(value.get('code', ''))",
            "                if rv is not None:",
            "                    return rv",
            "            except Exception:",
            "                pass",
            "            after = set(globals().keys())",
            "            created = [n for n in after - before if callable(globals().get(n))]",
            "            if len(created) == 1:",
            "                return globals()[created[0]]",
            "            return value",
            "        return value",
            "",
            "    if isinstance(value, (list, tuple)):",
            "        return func(value)",
            "    if isinstance(value, dict):",
            "        name = getattr(func, '__name__', '')",
            "        if getattr(func, '__is_dsl_component', False) or name.startswith('__comp_'):",
            "            return func(value)",
            "        try:",
            "            return func(**value)",
            "        except TypeError:",
            "            # Some builtins (like sorted) have a positional-only 'iterable'",
            "            # parameter. If passing as keywords fails, try calling with the",
            "            # iterable as the first positional argument and the rest as kws.",
            "            if 'iterable' in value:",
            "                iterable = value['iterable']",
            "                other = {k: v for k, v in value.items() if k != 'iterable'}",
            "                return func(iterable, **other)",
            "            # If dict has a single entry, try passing it as the positional arg",
            "            if len(value) == 1:",
            "                only = next(iter(value.values()))",
            "                return func(only)",
            "            raise",
            "    return func(value)",
            "",
        ]
    )

    # Helper to convert a non-callable 'key' value into a callable key function
    # when appropriate. This supports cases where the DSL provides a literal
    # ordering (list/tuple) or a string representation of one — we turn that
    # into a key function that maps items to their index in the provided order.
    code_lines.extend(
        [
            "def __ensure_key_callable(v):",
            "    try:",
            "        import ast",
            "        lit = None",
            "        if isinstance(v, str):",
            "            try:",
            "                lit = ast.literal_eval(v)",
            "            except Exception:",
            "                lit = None",
            "        else:",
            "            lit = v",
            "        if isinstance(lit, (list, tuple)):",
            "            index = {}",
            "            for i, item in enumerate(lit):",
            "                try:",
            "                    keyt = tuple(item) if isinstance(item, (list, tuple)) else item",
            "                except Exception:",
            "                    keyt = item",
            "                index[keyt] = i",
            "            def _key(x):",
            "                xt = tuple(x) if isinstance(x, (list, tuple)) else x",
            "                return index.get(xt, float('inf'))",
            "            return _key",
            "    except Exception:",
            "        pass",
            "    return v",
            "",
        ]
    )

    # Build a list of regex-backed components (pattern -> __comp_name)
    code_lines.append("import re")
    code_lines.append("__re_components = []")
    for cname, cobj in components.items():
        if getattr(cobj, "pattern", None):
            # repr(cobj.pattern) already yields a correctly-escaped Python
            # string literal (e.g. "'^(?P<name>\\w+)=$'"). Use that
            # directly with re.compile() rather than adding an `r` prefix
            # which would double-escape backslashes and break patterns
            # like "\w".
            code_lines.append(
                f"__re_components.append((re.compile({repr(cobj.pattern)}), '__comp_{cname}'))"
            )

    # Lookup helper: resolves a child name to a callable. It first prefers a
    # compiled component named __comp_<name>, then a top-level callable by that
    # name, and finally attempts to match regex-backed components. When a
    # regex component matches, it returns a small wrapper that injects the
    # captured groups into the child's default dict before invoking the
    # matched component.
    code_lines.append("def __lookup_child(name):")
    code_lines.append("    # direct compiled component")
    code_lines.append("    c = globals().get('__comp_' + name)")
    code_lines.append("    if c is not None:")
    code_lines.append("        return c")
    code_lines.append("    # top-level callable (e.g., builtins like sorted)")
    code_lines.append("    t = globals().get(name)")
    code_lines.append("    if callable(t):")
    code_lines.append("        return t")
    code_lines.append("    # check builtins (e.g., exec, sorted)")
    code_lines.append("    import builtins")
    code_lines.append("    b = getattr(builtins, name, None)")
    code_lines.append("    if callable(b):")
    code_lines.append("        return b")
    code_lines.append("    # try regex-backed components")
    code_lines.append("    for pat, comp_name in __re_components:")
    code_lines.append("        m = pat.match(name)")
    code_lines.append("        if m:")
    code_lines.append("            gd = m.groupdict()")
    code_lines.append("            def _reg_wrapper(default=None, *args, **kwargs):")
    code_lines.append(
        "                # If the incoming pipeline value is a dict, merge the captured"
    )
    code_lines.append(
        "                # groups into that dict so components can deref them from default."
    )
    code_lines.append(
        "                # Otherwise, keep the pipeline value as the default and pass"
    )
    code_lines.append(
        "                # captured groups via kwargs so __deref can find them there."
    )
    code_lines.append("                if isinstance(default, dict):")
    code_lines.append("                    d = dict(default)")
    code_lines.append("                    d.update(gd)")
    code_lines.append(
        "                    return globals()[comp_name](d, *args, **kwargs)"
    )
    code_lines.append("                kw = dict(kwargs)")
    code_lines.append("                kw.update(gd)")
    code_lines.append(
        "                return globals()[comp_name](default, *args, **kw)"
    )
    code_lines.append("            return _reg_wrapper")
    code_lines.append("    return None")

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
        # If the component was defined from a regex signature, expose a
        # wrapper that matches components by name at runtime. We'll create
        # a small dispatcher function that, when invoked as a child lookup,
        # will match the requested child name against the regex and call
        # the compiled component if it matches.
        # regex-backed components are handled by __lookup_child registry;
        # no per-component wrapper emitted here.
        body_fn = f"__body_{name}"

        # No runtime baked-in components; all components must come from YAML.
        # (Previously import_comp was hard-coded here; remove that special-case)

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
                    # Support keys that embed a child invocation, e.g. key(exec): ...
                    m = re.match(r"^([A-Za-z_]\w*)\(([^)]+)\)$", k)
                    if m:
                        key_name = m.group(1)
                        child = m.group(2)
                        code_lines.append(
                            f"        {repr(key_name)}: __ensure_key_callable(__invoke_child(globals().get('__comp_{child}', {child}), {_expr_for_value(v)})),"
                        )
                    else:
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
                    # If the inner string looks like an assignment of the form
                    # `$name = <expr>` then emit code that assigns into globals()
                    # using the evaluated name as key, rather than producing an
                    # invalid assignment expression inside a return.
                    m_assign = None
                    if isinstance(inner, str):
                        m_assign = re.match(r"^\s*\$([A-Za-z_]\w*)\s*=\s*(.+)$", inner)
                        m_assign_literal = re.match(
                            r"^\s*([A-Za-z_]\w*)\s*=\s*(.+)$", inner
                        )
                    if m_assign:
                        lhs_name = m_assign.group(1)
                        rhs_raw = m_assign.group(2)
                        rhs_expr = _replace_default_token(rhs_raw)
                        var_expr = f"__deref('{lhs_name}', default, args, kwargs)"
                        code_lines.append(
                            f"def {body_fn}(default=None, *args, **kwargs):"
                        )
                        code_lines.append(f"    globals()[{var_expr}] = ({rhs_expr})")
                        code_lines.append("    return default")
                        code_lines.append("")
                    elif m_assign_literal:
                        # LHS is a literal name; build a template for the RHS,
                        # exec the assignment, and return the assigned global.
                        lhs_name = m_assign_literal.group(1)
                        rhs_raw = m_assign_literal.group(2)
                        code_lines.append(
                            f"def {body_fn}(default=None, *args, **kwargs):"
                        )
                        code_lines.append("    import re as _re")
                        code_lines.append(f"    tpl = {repr(rhs_raw)}")
                        code_lines.append(
                            "    tpl = tpl.replace('{','{{').replace('}','}}')"
                        )
                        code_lines.append(
                            "    tpl = tpl.replace('&&', ' and ').replace('||', ' or ')"
                        )
                        code_lines.append("    tpl2_chars = []")
                        code_lines.append("    keys = []")
                        code_lines.append("    i = 0")
                        code_lines.append("    L = len(tpl)")
                        code_lines.append("    while i < L:")
                        code_lines.append("        ch = tpl[i]")
                        code_lines.append("        if ch == '\\\\' and i + 1 < L:")
                        code_lines.append("            tpl2_chars.append(tpl[i+1])")
                        code_lines.append("            i += 2")
                        code_lines.append("            continue")
                        code_lines.append("        if ch == '$':")
                        code_lines.append("            j = i + 1")
                        code_lines.append("            if j < L:")
                        code_lines.append(
                            "                # named placeholder starting with letter or underscore"
                        )
                        code_lines.append(
                            "                if tpl[j].isalpha() or tpl[j] == '_':"
                        )
                        code_lines.append("                    k = j")
                        code_lines.append(
                            "                    while k < L and (tpl[k].isalnum() or tpl[k] == '_'):"
                        )
                        code_lines.append("                        k += 1")
                        code_lines.append("                    name = tpl[j:k]")
                        code_lines.append(
                            "                    tpl2_chars.append('{' + name + '}')"
                        )
                        code_lines.append("                    if name not in keys:")
                        code_lines.append("                        keys.append(name)")
                        code_lines.append("                    i = k")
                        code_lines.append("                    continue")
                        code_lines.append(
                            "                # numeric placeholder like $0, $1, etc."
                        )
                        code_lines.append("                if tpl[j].isdigit():")
                        code_lines.append("                    k = j")
                        code_lines.append(
                            "                    while k < L and tpl[k].isdigit():"
                        )
                        code_lines.append("                        k += 1")
                        code_lines.append("                    name = tpl[j:k]")
                        code_lines.append(
                            "                    tpl2_chars.append('{' + name + '}')"
                        )
                        code_lines.append("                    if name not in keys:")
                        code_lines.append("                        keys.append(name)")
                        code_lines.append("                    i = k")
                        code_lines.append("                    continue")
                        code_lines.append("            tpl2_chars.append('$')")
                        code_lines.append("            i += 1")
                        code_lines.append("            continue")
                        code_lines.append("        tpl2_chars.append(ch)")
                        code_lines.append("        i += 1")
                        code_lines.append("    tpl2 = ''.join(tpl2_chars)")
                        code_lines.append("    mapping = {}")
                        code_lines.append("    for _k in keys:")
                        code_lines.append("        if _k == 'default':")
                        code_lines.append("            v = default")
                        code_lines.append("            mk = _k")
                        code_lines.append(
                            "        elif _k.startswith('n') and _k[1:].isdigit():"
                        )
                        code_lines.append(
                            "            v = __deref_index(_k[1:], default, args, kwargs)"
                        )
                        code_lines.append("            mk = _k")
                        code_lines.append("        elif _k.isdigit():")
                        code_lines.append(
                            "            v = __deref_index(_k, default, args, kwargs)"
                        )
                        code_lines.append("            mk = _k")
                        code_lines.append("        else:")
                        code_lines.append(
                            "            v = __deref(_k, default, args, kwargs)"
                        )
                        code_lines.append("            mk = _k")
                        code_lines.append("        if isinstance(v, int):")
                        code_lines.append("            mapping[mk] = str(v)")
                        code_lines.append(
                            r"        elif isinstance(v, str) and _re.match(r'^[+\-*/]$', v):"
                        )
                        code_lines.append("            mapping[mk] = v")
                        code_lines.append(
                            r"        elif isinstance(v, str) and _re.match(r'^-?\d+$', v):"
                        )
                        code_lines.append("            mapping[mk] = v")
                        code_lines.append("        else:")
                        code_lines.append("            mapping[mk] = repr(v)")
                        code_lines.append(
                            f"    code = {repr(lhs_name + ' = ')} + tpl2.format_map(mapping)"
                        )
                        code_lines.append("    try:")
                        code_lines.append("        rv = py(code)")
                        code_lines.append("    except Exception:")
                        code_lines.append("        pass")
                        code_lines.append(f"    return globals().get({repr(lhs_name)})")
                        code_lines.append("")
                    else:
                        # For quoted string bodies that contain $-placeholders
                        # but are not the simple `$name = <expr>` form, build
                        # a runtime template substitution. We map placeholders
                        # to safe tokens (operators, numeric literals) or
                        # repr()-escaped Python literals, then exec the
                        # resulting source in globals(). This keeps the
                        # substitution simple while avoiding arbitrary token
                        # injection for non-whitelisted placeholder values.
                        code_lines.append(
                            f"def {body_fn}(default=None, *args, **kwargs):"
                        )
                        code_lines.append("    import re as _re")
                        code_lines.append(f"    tpl = {repr(inner)}")
                        code_lines.append(
                            "    # escape any braces so format_map() works"
                        )
                        code_lines.append(
                            "    tpl = tpl.replace('{','{{').replace('}','}}')"
                        )
                        code_lines.append(
                            "    tpl = tpl.replace('&&', ' and ').replace('||', ' or ')"
                        )
                        code_lines.append(
                            "    # simple parser to replace $name with {name} and collect keys"
                        )
                        code_lines.append("    tpl2_chars = []")
                        code_lines.append("    keys = []")
                        code_lines.append("    i = 0")
                        code_lines.append("    L = len(tpl)")
                        code_lines.append("    while i < L:")
                        code_lines.append("        ch = tpl[i]")
                        code_lines.append(
                            "        # handle escaped characters (\\\\x -> x)"
                        )
                        code_lines.append("        if ch == '\\\\' and i + 1 < L:")
                        code_lines.append("            tpl2_chars.append(tpl[i+1])")
                        code_lines.append("            i += 2")
                        code_lines.append("            continue")
                        code_lines.append("        if ch == '$':")
                        code_lines.append("            j = i + 1")
                        code_lines.append(
                            "            if j < L and (tpl[j].isalpha() or tpl[j] == '_'):"
                        )
                        code_lines.append("                k = j")
                        code_lines.append(
                            "                while k < L and (tpl[k].isalnum() or tpl[k] == '_'):"
                        )
                        code_lines.append("                    k += 1")
                        code_lines.append("                name = tpl[j:k]")
                        code_lines.append(
                            "                tpl2_chars.append('{' + name + '}')"
                        )
                        code_lines.append("                if name not in keys:")
                        code_lines.append("                    keys.append(name)")
                        code_lines.append("                i = k")
                        code_lines.append("                continue")
                        code_lines.append(
                            "            # numeric placeholder like $0, $1, etc."
                        )
                        code_lines.append("            if j < L and tpl[j].isdigit():")
                        code_lines.append("                k = j")
                        code_lines.append(
                            "                while k < L and tpl[k].isdigit():"
                        )
                        code_lines.append("                    k += 1")
                        code_lines.append("                name = tpl[j:k]")
                        code_lines.append(
                            "                tpl2_chars.append('{' + 'n' + name + '}')"
                        )
                        code_lines.append("                if 'n' + name not in keys:")
                        code_lines.append("                    keys.append('n' + name)")
                        code_lines.append("                i = k")
                        code_lines.append("                continue")
                        code_lines.append("            else:")
                        code_lines.append("                tpl2_chars.append('$')")
                        code_lines.append("                i += 1")
                        code_lines.append("                continue")
                        code_lines.append("        tpl2_chars.append(ch)")
                        code_lines.append("        i += 1")
                        code_lines.append("    tpl2 = ''.join(tpl2_chars)")
                        code_lines.append(
                            "    # numeric placeholders were encoded as {n0},{n1} etc.\n"
                            "    # ensure tpl2 uses the n-prefixed names for any numeric fields"
                        )
                        # The 'n' prefix is used to avoid Python's str.format positional
                        # field parsing for numeric field names. A bare '{0}' is treated
                        # as a positional index by str.format(), but we need to call
                        # format_map(mapping) with named keys. Encoding numeric
                        # placeholders as '{n0}' lets us store their values under the
                        # mapping key 'n0' and still have format_map() substitute
                        # them as named fields. At runtime we resolve 'n0' via
                        # __deref_index('0', ...).
                        code_lines.append("    mapping = {}")
                        code_lines.append("    for _k in keys:")
                        code_lines.append("        if _k == 'default':")
                        code_lines.append("            v = default")
                        code_lines.append("            mk = _k")
                        code_lines.append(
                            "        elif _k.startswith('n') and _k[1:].isdigit():"
                        )
                        code_lines.append(
                            "            v = __deref_index(_k[1:], default, args, kwargs)"
                        )
                        code_lines.append("            mk = _k")
                        code_lines.append("        elif _k.isdigit():")
                        code_lines.append(
                            "            v = __deref_index(_k, default, args, kwargs)"
                        )
                        code_lines.append("            mk = _k")
                        code_lines.append("        else:")
                        code_lines.append(
                            "            v = __deref(_k, default, args, kwargs)"
                        )
                        code_lines.append("            mk = _k")
                        # ints -> raw numeric token
                        code_lines.append("        if isinstance(v, int):")
                        code_lines.append("            mapping[mk] = str(v)")
                        # operator strings -> raw token (whitelisted)
                        code_lines.append(
                            r"        elif isinstance(v, str) and _re.match(r'^[+\-*/]$', v):"
                        )
                        code_lines.append("            mapping[mk] = v")
                        # numeric strings -> raw numeric token
                        code_lines.append(
                            r"        elif isinstance(v, str) and _re.match(r'^-?\d+$', v):"
                        )
                        code_lines.append("            mapping[mk] = v")
                        # otherwise inject Python literal
                        code_lines.append("        else:")
                        code_lines.append("            mapping[mk] = repr(v)")
                        code_lines.append("    code = tpl2.format_map(mapping)")
                        code_lines.append(
                            "    # If the template produced an expression, try to eval and return it"
                        )
                        code_lines.append("    try:")
                        code_lines.append("        rv = py(code)")
                        code_lines.append("        if rv is not None:")
                        code_lines.append("            return rv")
                        code_lines.append("    except Exception:")
                        code_lines.append("        pass")
                        code_lines.append("    before = set(globals().keys())")
                        code_lines.append("    try:")
                        code_lines.append("        py(code)")
                        code_lines.append("    except Exception:")
                        code_lines.append("        pass")
                        code_lines.append("    after = set(globals().keys())")
                        code_lines.append(
                            "    created = [n for n in after - before if callable(globals().get(n))]"
                        )
                        code_lines.append("    if len(created) == 1:")
                        code_lines.append("        return globals()[created[0]]")
                        code_lines.append(
                            "    created_noncall = [n for n in after - before if not callable(globals().get(n))]"
                        )
                        code_lines.append("    if len(created_noncall) == 1:")
                        code_lines.append(
                            "        return globals()[created_noncall[0]]"
                        )
                else:
                    expr = _replace_default_token(comp.body)
                    code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                    code_lines.append(f"    return ({expr})")
                    code_lines.append("")
        else:
            body = _replace_default_token(textwrap.dedent(comp.body))
            # Try a limited compile-time normalization for reversed join
            # patterns (value.join(', ')) so code embedded from multi-line
            # YAML block bodies doesn't contain `.join` calls on list
            # objects (which would raise AttributeError). Only perform the
            # join() transform here; other DSL literal normalization is
            # handled at runtime.
            try:
                norm = _normalize_source(body)
                if isinstance(norm, str):
                    body = norm
            except Exception:
                pass
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
            # Resolve child names via __lookup_child which handles compiled
            # components, top-level callables, and regex-backed components.
            code_lines.append(f"    _child_callable = __lookup_child({repr(child)})")
            code_lines.append(f"    if _child_callable is None:")
            code_lines.append(f"        _child_callable = globals().get({repr(child)})")
            code_lines.append(f"    if _child_callable is None:")
            code_lines.append(
                f"        raise KeyError({repr('child ' + child + ' not found')})"
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
    # expose the actual generated code for debugging
    ns["_dsl_generated_code"] = code
    # ensure the py helper is available as a top-level callable so YAML can
    # refer to it as a child: e.g., main(py): <code>
    if "py" in ns:
        ns["py"] = ns["py"]
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
