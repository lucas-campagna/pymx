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
    # Map of component name -> metadata for error reporting
    comp_map: Dict[str, Dict[str, Any]] = {}

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
            "    if func is exec or func is eval or getattr(func, '__name__', '') == 'py':",
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
    # Mapping from component name -> body function variable. Wrappers will
    # call the body via this lookup to avoid relying on a stable function
    # name (prevents accidental duplicate-def collisions from changing which
    # body is invoked at runtime).
    code_lines.append("__body_lookup = {}")
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

    # Build template mappings: map base component name -> template component
    # Enforce compile-time rule: a component can only have one template/parent
    templates_by_base: Dict[str, List[str]] = {}
    # First, collect templates and normal components
    for cname, cobj in components.items():
        lvl = getattr(cobj, "template_level", 0)
        if lvl and lvl > 0:
            # template component; determine its base target(s)
            if getattr(cobj, "pattern", None):
                # regex template: will be applied to matching component names
                # recorded as special entry keyed by the template cname
                templates_by_base.setdefault("__regex_templates", []).append(cname)
            else:
                base = getattr(cobj, "base_name", None)
                if base is None:
                    continue
                templates_by_base.setdefault(base, []).append(cname)

    # For regex templates, resolve which concrete components they apply to
    regex_templates = templates_by_base.pop("__regex_templates", [])
    if regex_templates:
        for rt in regex_templates:
            pat = components[rt].pattern
            for target_name, target_comp in list(components.items()):
                # skip templates themselves
                if getattr(target_comp, "template_level", 0) > 0:
                    continue
                # pattern is matched against the raw component name (base)
                if re.match(pat, target_comp.name):
                    templates_by_base.setdefault(target_comp.name, []).append(rt)

    # Enforce compile-time constraints: at most one template per component
    for base, tpl_list in templates_by_base.items():
        if len(tpl_list) > 1:
            raise ValueError(f"Component {base!r} has multiple templates: {tpl_list}")

    for name, comp in components.items():
        # Record the start line in the generated code for this component
        start_line = len(code_lines) + 1
        # Track whether we've emitted a body function for this component
        # Some generator paths can attempt to emit multiple bodies for the
        # same logical component (this was a source of duplicate `def
        # __body_...` occurrences). Guard so only the first emission
        # actually writes the body; later branches must not re-emit.
        emitted_body = False
        # If the component was defined from a regex signature, expose a
        # wrapper that matches components by name at runtime. We'll create
        # a small dispatcher function that, when invoked as a child lookup,
        # will match the requested child name against the regex and call
        # the compiled component if it matches.
        # regex-backed components are handled by __lookup_child registry;
        # no per-component wrapper emitted here.
        body_fn = f"__body_{name}"
        # Debug prints removed - keep concise generation
        # Avoid emitting the same body function name multiple times. In some
        # generator paths a name collision could occur (previously observed
        # as duplicate defs like __body___tpl_2). If a collision is detected
        # pick a unique alternative name so each component's body is bound to
        # its own function and wrappers call the correct one.
        # Track emitted body function names; duplicate defs indicate a
        # generator bug. Treat duplicates as a hard error to avoid subtle
        # runtime behavior differences caused by silent renaming.
        if not hasattr(compile_namespace, "_emitted_body_fns"):
            compile_namespace._emitted_body_fns = set()
        if body_fn in compile_namespace._emitted_body_fns:
            raise RuntimeError(f"Duplicate emitted body function name: {body_fn}")
        compile_namespace._emitted_body_fns.add(body_fn)

        # No runtime baked-in components; all components must come from YAML.
        # (Previously import_comp was hard-coded here; remove that special-case)

        if comp.body_type == "expr":
            raw = comp.body
            try:
                lit = _ast.literal_eval(raw)
            except Exception:
                lit = None

            if isinstance(lit, dict):
                if not emitted_body:
                    code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                    emitted_body = True
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
                if not emitted_body:
                    code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                    emitted_body = True
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
                        if not emitted_body:
                            code_lines.append(
                                f"def {body_fn}(default=None, *args, **kwargs):"
                            )
                            emitted_body = True
                        code_lines.append(f"    globals()[{var_expr}] = ({rhs_expr})")
                        code_lines.append("    return default")
                        code_lines.append("")
                    elif m_assign_literal:
                        # LHS is a literal name; build a template for the RHS,
                        # exec the assignment, and return the assigned global.
                        lhs_name = m_assign_literal.group(1)
                        rhs_raw = m_assign_literal.group(2)
                        if not emitted_body:
                            code_lines.append(
                                f"def {body_fn}(default=None, *args, **kwargs):"
                            )
                            emitted_body = True
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
                        # If the value is an identifier-like string (eg. a name
                        # captured by a regex) inject it raw (no quotes) so it can
                        # be used as a variable name in generated code.
                        code_lines.append(
                            r"        elif isinstance(v, str) and _re.match(r'^[A-Za-z_]\w*$', v):"
                        )
                        code_lines.append("            mapping[mk] = v")
                        code_lines.append("        else:")
                        code_lines.append("            mapping[mk] = repr(v)")
                        code_lines.append(
                            f"    code = {repr(lhs_name + ' = ')} + tpl2.format_map(mapping) + '\\n' + {repr(lhs_name)}"
                        )
                        code_lines.append("    try:")
                        code_lines.append("        rv = py(code)")
                        code_lines.append("        if rv is not None:")
                        code_lines.append("            return rv")
                        code_lines.append("    except Exception:")
                        code_lines.append("        pass")
                        code_lines.append(f"    _gv = globals().get({repr(lhs_name)})")
                        code_lines.append("    if _gv is not None:")
                        code_lines.append("        return _gv")
                        # Fallback: return the substituted code string itself
                        code_lines.append("    return code")
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
                        if not emitted_body:
                            code_lines.append(
                                f"def {body_fn}(default=None, *args, **kwargs):"
                            )
                            emitted_body = True
                        code_lines.append("    import re as _re")
                        code_lines.append(f"    tpl = {repr(inner)}")
                        # Treat escaped dollar sequences (\$) as a literal dollar
                        # so they are not interpreted as placeholders during
                        # template substitution. We use a sentinel token that
                        # is restored after format_map().
                        code_lines.append(
                            "    tpl = tpl.replace('\\\\$', '__DSL_ESC_DOLLAR__')"
                        )
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
                            "    code = code.replace('__DSL_ESC_DOLLAR__', '$')"
                        )
                        code_lines.append(
                            "    m = _re.match(r'^\\s*([A-Za-z_]\\w*)\\s*=', code)"
                        )
                        code_lines.append("    if m:")
                        code_lines.append("        code = code + '\\n' + m.group(1)")
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
                        # Fallback: when execution produced nothing useful, return the
                        # substituted template text so templates that produce plain
                        # strings still yield a value instead of None.
                        code_lines.append("    return code")
                else:
                    expr = _replace_default_token(comp.body)
                    if not emitted_body:
                        code_lines.append(
                            f"def {body_fn}(default=None, *args, **kwargs):"
                        )
                        emitted_body = True
                    code_lines.append(f"    return ({expr})")
                    code_lines.append("")
        else:
            # If the original YAML body contains $-placeholders and this was
            # a block-style body, treat it as a runtime template: build a
            # substituted code string at runtime (so operators, names, and
            # numeric tokens can be injected safely) and exec() it in
            # globals(). This covers regex-backed components and other DSL
            # snippets that embed $-tokens in block code.
            orig_body = getattr(comp, "orig_body", None)
            # Track whether we emitted the runtime-template form for this
            # block body (orig_body contained $ and we wrote a custom
            # runtime substitution function). When true we must avoid
            # further processing that expects `body` to be defined.
            handled_runtime_template = False
            body = None
            if orig_body is not None and "$" in orig_body:
                # Emit a runtime template substitution similar to the
                # quoted-string template branch but tailored for block bodies
                # where the produced code should be exec()'d and the
                # component returns the incoming default.
                if not emitted_body:
                    code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                    emitted_body = True
                code_lines.append("    import re as _re")
                # Normalize the block source if possible (fix patterns like
                # value.join(', ') -> ', '.join(value)) before embedding the
                # template so runtime exec() sees the corrected code.
                tpl_src = textwrap.dedent(orig_body)
                # Apply a lightweight join() normalization for patterns like
                # `value.join(', ')` -> `', '.join(value)` so lists used in
                # templates behave as expected. This runs before the more
                # expensive AST-based normalization and works even when the
                # body contains $-placeholders which would break parsing.
                try:
                    tpl_src = re.sub(
                        r"(\b[A-Za-z_]\w*)\.join\(\s*('(?:\\'|[^'])*'|\"(?:\\\"|[^\"])*\")\s*\)",
                        lambda m: f"{m.group(2)}.join({m.group(1)})",
                        tpl_src,
                    )
                except Exception:
                    pass
                try:
                    norm_tpl = _normalize_source(tpl_src)
                    if isinstance(norm_tpl, str):
                        tpl_src = norm_tpl
                except Exception:
                    pass
                code_lines.append(f"    tpl = {repr(tpl_src)}")
                # Same escaped-dollar handling for block templates.
                code_lines.append(
                    "    tpl = tpl.replace('\\\\$', '__DSL_ESC_DOLLAR__')"
                )
                code_lines.append("    # escape any braces so format_map() works")
                code_lines.append("    tpl = tpl.replace('{','{{').replace('}','}}')")
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
                code_lines.append("        # handle escaped characters (\\\\x -> x)")
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
                code_lines.append("                tpl2_chars.append('{' + name + '}')")
                code_lines.append("                if name not in keys:")
                code_lines.append("                    keys.append(name)")
                code_lines.append("                i = k")
                code_lines.append("                continue")
                code_lines.append("            # numeric placeholder like $0, $1, etc.")
                code_lines.append("            if j < L and tpl[j].isdigit():")
                code_lines.append("                k = j")
                code_lines.append("                while k < L and tpl[k].isdigit():")
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
                code_lines.append("            v = __deref(_k, default, args, kwargs)")
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
                # identifier-like captured group values (eg. names) should be
                # injected raw (no quotes) so they can act as variable names
                # in the emitted code (eg. `sorted_list = ...`).
                code_lines.append(
                    r"        elif isinstance(v, str) and _re.match(r'^[A-Za-z_]\w*$', v):"
                )
                code_lines.append("            mapping[mk] = v")
                code_lines.append("        else:")
                code_lines.append("            mapping[mk] = repr(v)")
                code_lines.append("    code = tpl2.format_map(mapping)")
                code_lines.append("    code = code.replace('__DSL_ESC_DOLLAR__', '$')")
                # If this looks like an assignment to a simple global name
                # prefer executing it via exec(..., globals()) first so the
                # module-level globals are deterministically mutated. Then
                # return the assigned global's value. This avoids relying on
                # py() which may evaluate in ways that don't persist side-
                # effects consistently for assignment-heavy templates.
                code_lines.append("    m = _re.match(r'^\s*([A-Za-z_]\w*)\s*=', code)")
                code_lines.append("    if m:")
                code_lines.append("        try:")
                code_lines.append("            exec(code, globals())")
                code_lines.append("        except Exception:")
                code_lines.append("            pass")
                code_lines.append("        _vn = m.group(1)")
                code_lines.append("        if _vn in globals():")
                code_lines.append("            return globals().get(_vn)")
                # If exec didn't produce the named global, fall back to py()
                # which will eval the last expression if possible.
                code_lines.append("    try:")
                code_lines.append("        rv = py(code)")
                code_lines.append("        if rv is not None:")
                code_lines.append("            return rv")
                code_lines.append("    except Exception:")
                code_lines.append("        pass")
                # As a final fallback, run exec and inspect created names.
                code_lines.append("    before = set(globals().keys())")
                code_lines.append("    try:")
                code_lines.append("        exec(code, globals())")
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
                code_lines.append("        return globals()[created_noncall[0]]")
                # If we matched an assignment target but couldn't find a new
                # created name, prefer returning the (possibly-updated)
                # global value if present.
                code_lines.append("    if m:")
                code_lines.append("        _vn = m.group(1)")
                code_lines.append("        if _vn in globals():")
                code_lines.append("            return globals().get(_vn)")
                # Default: return the incoming default value
                code_lines.append("    return default")
                code_lines.append("")
                # We've emitted the runtime-template body for this component.
                handled_runtime_template = True
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
            # Only process a pre-specified `body` when we did not emit the
            # runtime-template form above. When orig_body contained `$` we
            # generated code directly and `body` may be None in this
            # branch; guard to avoid referencing an unassigned local.
            if (
                not handled_runtime_template
                and body is not None
                and re.search(r"^\s*def\s+__body\s*\(", body, flags=re.MULTILINE)
            ):
                # Replace the placeholder `def __body(...` with our computed
                # body name. Only perform the replacement if we haven't
                # already emitted a body for this component.
                body_replaced = re.sub(
                    r"^\s*def\s+__body\s*\(",
                    f"def {body_fn}(",
                    body,
                    count=1,
                    flags=re.MULTILINE,
                )
                if not emitted_body:
                    code_lines.extend(body_replaced.splitlines())
                    code_lines.append("")
                    emitted_body = True
            elif (
                not handled_runtime_template
                and body is not None
                and re.search(
                    r"^\s*(def|class|import|from)\b", body, flags=re.MULTILINE
                )
            ):
                code_lines.extend(body.splitlines())
                code_lines.append("")
                if not emitted_body:
                    code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                    code_lines.append("    return default")
                    code_lines.append("")
                    emitted_body = True
            elif not handled_runtime_template and body is not None:
                first_nonblank = next(
                    (ln for ln in body.splitlines() if ln.strip()), ""
                )
                if first_nonblank.lstrip().startswith(
                    ("def ", "class ", "import ", "from ")
                ):
                    code_lines.extend(body.splitlines())
                    code_lines.append("")
                    if not emitted_body:
                        code_lines.append(
                            f"def {body_fn}(default=None, *args, **kwargs):"
                        )
                        code_lines.append("    return None")
                        code_lines.append("")
                        emitted_body = True
                else:
                    if not emitted_body:
                        code_lines.append(
                            f"def {body_fn}(default=None, *args, **kwargs):"
                        )
                        emitted_body = True
                    stripped_lines = [ln for ln in body.splitlines() if ln.strip()]
                    if len(stripped_lines) == 1 and not stripped_lines[
                        0
                    ].lstrip().startswith(
                        ("def ", "class ", "import ", "from ", "return ")
                    ):
                        single = stripped_lines[0]
                        # If the single line is a DSL-style component call like
                        # `name(...)` treat it specially: don't exec the raw
                        # string (which may contain Python keywords as kw names),
                        # instead route it through __invoke_child with the
                        # py helper which knows how to parse and invoke DSL
                        # component-call syntax safely.
                        mcall = re.match(r"^\s*([A-Za-z_]\w*)\s*\((.*)\)\s*$", single)
                        if mcall:
                            code_lines.append(f"    try:")
                            code_lines.append(
                                f"        _rv = __invoke_child(globals().get('py'), {repr(single)})"
                            )
                            code_lines.append("        return _rv")
                            code_lines.append("    except Exception:")
                            code_lines.append("        raise")
                        else:
                            # If the single line looks like an assignment and
                            # the component body was a block (so assignments
                            # should mutate module globals), handle two
                            # special cases:
                            # 1) LHS is a deref() placeholder (produced by
                            #    _replace_default_token for `$name = ...`). In
                            #    that situation we must assign into
                            #    globals()[__deref(...)] instead of trying to
                            #    assign to the function-call-like expression.
                            # 2) Otherwise fall back to exec(..., globals()).
                            if (
                                comp.body_type == "block"
                                and "=" in single
                                and "==" not in single
                                and ":=" not in single
                            ):
                                # detect deref-style LHS produced by
                                # _replace_default_token -> __deref('name', ...)
                                m_assign_deref = re.match(
                                    r"^\s*__deref\('([A-Za-z_]\w*)',\s*default,\s*args,\s*kwargs\)\s*=\s*(.+)$",
                                    single,
                                )
                                if m_assign_deref:
                                    lhs_name = m_assign_deref.group(1)
                                    rhs_raw = m_assign_deref.group(2)
                                    var_expr = (
                                        f"__deref('{lhs_name}', default, args, kwargs)"
                                    )
                                    code_lines.append(
                                        f"    globals()[{var_expr}] = ({rhs_raw})"
                                    )
                                    code_lines.append("    return default")
                                else:
                                    code_lines.append(
                                        f"    exec({repr(single)}, globals())"
                                    )
                                    code_lines.append("    return default")
                            elif (
                                "=" in single
                                and "==" not in single
                                and ":=" not in single
                            ):
                                # For non-block (expr-derived) single-line assignments
                                # prefer emitting the statement and returning default.
                                code_lines.append(f"    {single}")
                                code_lines.append("    return default")
                            else:
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
            else:
                # No body to process here (either we emitted a runtime-template
                # form above or body was None). Ensure a body function exists so
                # wrappers can call it; if one wasn't emitted earlier, emit a
                # minimal body that returns the incoming default.
                if not emitted_body:
                    code_lines.append(f"def {body_fn}(default=None, *args, **kwargs):")
                    code_lines.append("    return default")
                    code_lines.append("")
                    emitted_body = True

        # Register the emitted body function into the runtime lookup so
        # wrappers will call the correct body even if function names collide.
        code_lines.append(f"__body_lookup[{repr(name)}] = {body_fn}")
        code_lines.append(f"def __comp_{name}(default=None, *args, **kwargs):")
        code_lines.append(
            f"    result = __body_lookup[{repr(name)}](default, *args, **kwargs)"
        )

        # If this component has a parent template defined, invoke it once
        # After the base body returns but before children are invoked. If the
        # component was marked with pre_template_first the first child will be
        # called before the parent template is applied.
        tpl = None
        pre_first = getattr(comp, "pre_template_first", False)
        first_child_name = comp.children[0] if comp.children else None
        has_tpl = name in templates_by_base

        # Only perform the pre-template invocation of the first child when a
        # parent template exists for this component. If no template exists the
        # trailing '!' is ignored and the child will be invoked in normal
        # order below.
        if has_tpl and pre_first and first_child_name is not None:
            # call the first child before applying the parent template
            code_lines.append(
                f"    # pre-template: invoke first child {first_child_name} before parent for {name}"
            )
            code_lines.append(
                f"    _pre_child = __lookup_child({repr(first_child_name)})"
            )
            code_lines.append(f"    if _pre_child is None:")
            code_lines.append(
                f"        _pre_child = globals().get({repr(first_child_name)})"
            )
            code_lines.append(f"    if _pre_child is None:")
            code_lines.append(
                f"        raise KeyError({repr('child ' + first_child_name + ' not found')})"
            )
            code_lines.append(f"    result = __invoke_child(_pre_child, result)")

        if has_tpl:
            tpl = templates_by_base[name][0]
            # call the template once and replace the current result
            code_lines.append(
                f"    # apply parent template {tpl} once for component {name}"
            )
            code_lines.append(f"    _tpl_callable = __lookup_child({repr(tpl)})")
            code_lines.append(f"    if _tpl_callable is None:")
            code_lines.append(f"        _tpl_callable = globals().get({repr(tpl)})")
            code_lines.append(f"    if _tpl_callable is None:")
            code_lines.append(
                f"        raise KeyError({repr('parent template ' + tpl + ' not found')})"
            )
            code_lines.append(f"    result = __invoke_child(_tpl_callable, result)")

        for i, child in enumerate(comp.children):
            # If we already invoked the first child before applying the
            # parent template due to the '!' operator, skip it here so it's
            # not called twice. Only skip when a template actually exists
            # for this component; when no template exists the trailing '!' is
            # ignored and the child should be invoked in normal order.
            if pre_first and i == 0 and has_tpl:
                continue
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
        # record the emitted range for mapping back to DSL source
        end_line = len(code_lines)
        comp_map[name] = {
            "start": start_line,
            "end": end_line,
            "body": comp.body,
            "body_type": comp.body_type,
            "source": getattr(comp, "source", None),
            "orig_start": getattr(comp, "orig_start", None),
            "orig_body": getattr(comp, "orig_body", None),
            "orig_key": getattr(comp, "orig_key", None),
        }
        # Debug: emit a compact summary of the generated snippet for this
        # component so we can map duplicate def occurrences back to DSL.
        # Range debug prints removed

    code = "\n".join(code_lines)
    # Defensive check: ensure we don't emit duplicate body function defs
    # which indicate a generator logic bug (one def per body_fn expected).
    dup_pat = re.compile(r"^def\s+(__body_[A-Za-z0-9_]+)\(", flags=re.MULTILINE)
    found = dup_pat.findall(code)
    if found:
        from collections import Counter

        counts = Counter(found)
        dups = [n for n, c in counts.items() if c > 1]
        if dups:
            # Write the full generated code to /tmp for inspection and
            # continue (temporary diagnostic mode). In normal operation we
            # want to raise here, but for debugging allow execution so we
            # can inspect the resulting namespace.
            try:
                with open("/tmp/dsl_generated_code_full.py", "w") as f:
                    f.write(code)
            except Exception:
                pass
            print(
                f"WARNING: Duplicate body function definitions emitted: {dups}",
                file=sys.stderr,
            )

    ns: Dict[str, Any] = {}
    exec(compile(code, "<dsl-compiled>", "exec"), ns)
    # expose the actual generated code for debugging
    ns["_dsl_generated_code"] = code
    # Attach component mapping for error reporting (component -> start/end in
    # generated code lines and original YAML source where available).
    ns["_dsl_component_map"] = comp_map
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
    import sys, traceback

    try:
        return func(default)
    except Exception:
        exc_type, exc_value, tb = sys.exc_info()
        # Format a user-facing DSL-aware error message and re-raise the same
        # exception type with the augmented message, preserving traceback.
        try:
            extra = _format_dsl_exception(ns, exc_type, exc_value, tb)
            new_msg = f"{exc_value}\n\n{extra}"
            new_exc = exc_type(new_msg)
            raise new_exc.with_traceback(tb)
        except Exception:
            # If formatting fails for any reason, re-raise original
            raise


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


def _format_dsl_exception(
    ns: Dict[str, Any], exc_type, exc_value, tb, context_lines: int = 3
) -> str:
    import traceback, os

    # Locate the last frame generated from the DSL-compiled module
    extracted = traceback.extract_tb(tb)
    dsl_frame = None
    for fr in reversed(extracted):
        if fr.filename == "<dsl-compiled>":
            dsl_frame = fr
            break
    if dsl_frame is None:
        return "(no DSL frame in traceback)"

    lineno = dsl_frame.lineno
    comp_map = ns.get("_dsl_component_map", {})
    # Find component containing this line
    found = None
    for cname, meta in comp_map.items():
        if meta["start"] <= lineno <= meta["end"]:
            found = (cname, meta)
            break
    if found is None:
        # fallback: nearest preceding component
        candidates = [
            (m["start"], n, m) for n, m in comp_map.items() if m["start"] <= lineno
        ]
        if candidates:
            _, cname, meta = max(candidates)
            found = (cname, meta)
    if found is None:
        return f"Error at generated code line {lineno} (no component mapping available)"

    cname, meta = found
    source = meta.get("source")
    orig_start = meta.get("orig_start")
    orig_body = meta.get("orig_body")
    body_type = meta.get("body_type")

    lines = []
    lines.append(
        f"Error executing DSL component '{cname}': {exc_type.__name__}: {exc_value}"
    )
    if source:
        lines.append(f"Source: {source}")
    # Show DSL call stack (scan traceback for other DSL frames)
    dsl_stack = []
    for fr in extracted:
        if fr.filename == "<dsl-compiled>":
            # map to component name if possible
            ln = fr.lineno
            for n, m in comp_map.items():
                if m["start"] <= ln <= m["end"]:
                    dsl_stack.append((n, ln - m["start"]))
                    break
    if dsl_stack:
        lines.append("")
        lines.append("DSL call stack (outer -> inner):")
        for n, off in dsl_stack:
            lines.append(f"  - {n} (compiled line offset {off})")

    # Show a snippet from original YAML body if available
    if orig_body:
        body_lines = orig_body.splitlines()
        # Determine the offending line within the body if orig_start available
        if orig_start is not None:
            # attempt to map generated lineno to orig line; this is best-effort
            # If the orig_start is known, assume the first line of the generated
            # component body corresponds to orig_start.
            gen_start = meta.get("start")
            offending_generated_offset = lineno - gen_start
            # clamp to body length
            idx = max(0, min(len(body_lines) - 1, offending_generated_offset))
        else:
            idx = 0
        start = max(0, idx - context_lines)
        end = min(len(body_lines), idx + context_lines + 1)
        lines.append("")
        lines.append("DSL source (context):")
        for i in range(start, end):
            prefix = ">> " if i == idx else "   "
            lines.append(f"{prefix}{i + 1 + (orig_start or 0):4d}: {body_lines[i]}")

    # Optionally include the generated Python lines when DSL_DEBUG=1
    try:
        if os.environ.get("DSL_DEBUG") == "1":
            gen = ns.get("_dsl_generated_code", "")
            gen_lines = gen.splitlines()
            gstart = max(0, lineno - context_lines - 1)
            gend = min(len(gen_lines), lineno + context_lines)
            lines.append("")
            lines.append("Generated Python (context):")
            for i in range(gstart, gend):
                prefix = ">> " if i == lineno - 1 else "   "
                lines.append(f"{prefix}{i + 1:4d}: {gen_lines[i]}")
    except Exception:
        pass

    return "\n".join(lines)


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
