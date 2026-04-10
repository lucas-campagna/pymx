from dataclasses import dataclass
from typing import List, Dict, Optional
import re
import yaml
import textwrap


@dataclass
class Component:
    name: str
    children: List[str]
    body: str
    body_type: str  # 'expr' or 'block'
    pattern: Optional[str] = None
    # Optional origin information for improved error reporting
    source: Optional[str] = None
    orig_start: Optional[int] = None
    orig_body: Optional[str] = None
    orig_key: Optional[str] = None
    # Template/inheritance support
    template_level: int = 0
    # base_name is the raw component name this component is associated with
    # For templates this is the base signature (without leading $), for normal
    # components it's the component name itself.
    base_name: Optional[str] = None


def parse_components(
    yaml_text: str, source: Optional[str] = None
) -> Dict[str, Component]:
    """Parse top-level components from YAML text.

    Each top-level key is a component signature `name(child|other)` or `name`.
    The value is the body (string or YAML scalar). Returns a mapping of name->Component.
    """
    try:
        loaded = yaml.safe_load(yaml_text)
    except Exception:
        # Some test fixtures use unquoted single-line Python-like values
        # (eg. `main: lambda x: x*2`) which yaml.safe_load rejects. As a
        # fallback parse the top-level mapping manually for simple cases.
        loaded = {}
        for ln in yaml_text.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            val = v.strip()
            # If the fallback value is quoted, unquote it so both quoted
            # and unquoted single-line values compare equivalently to the
            # test fixtures' expectations.
            if len(val) >= 2 and (val[0] == val[-1]) and val[0] in ('"', "'"):
                val = val[1:-1]
            loaded[k.strip()] = val
    if not isinstance(loaded, dict):
        raise ValueError("YAML root must be a mapping of component_name: body")
    comps: Dict[str, Component] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise ValueError("component name must be a string")
        ks = key.strip()
        # detect leading $ characters (templates)
        tpl_level = 0
        while tpl_level < len(ks) and ks[tpl_level] == "$":
            tpl_level += 1
        base_sig = ks[tpl_level:]
        # regex-style signature: /pattern/ optionally followed by (children)
        if base_sig.startswith("/"):
            last = base_sig.rfind("/")
            if last <= 0:
                raise ValueError(f"Invalid regex component signature: {key!r}")
            pattern = base_sig[1:last]
            remainder = base_sig[last + 1 :]
            cm = re.match(r"^\((?P<children>[^)]+)\)$", remainder)
            children_raw = cm.group("children") if cm else None
            children = (
                [c.strip() for c in children_raw.split("|") if c.strip()]
                if children_raw
                else []
            )
            # generate a safe name for this regex component
            name = f"__re_{len(comps)}"
            # Preserve multi-line YAML scalars as raw block bodies so the
            # compiler can embed them as a code block. For single-line
            # scalars keep the Python literal representation (repr) so the
            # compiler can literal_eval them when appropriate.
            if isinstance(value, str) and "\n" in value:
                body_text = value
                body_type = "block"
            else:
                body_text = repr(value)
                body_type = "expr"
            comps[name] = Component(
                name=name,
                children=children,
                body=body_text,
                body_type=body_type,
                pattern=pattern,
                source=source,
                orig_key=ks,
                template_level=tpl_level,
                base_name=(None if tpl_level == 0 else base_sig[1:last]),
            )
        else:
            m = re.match(
                r"^(?P<name>[A-Za-z_]\w*)(?:\((?P<children>[^)]+)\))?$", base_sig
            )
            if not m:
                raise ValueError(f"Invalid component signature: {key!r}")
            name = m.group("name")
            children_raw = m.group("children")
            children = (
                [c.strip() for c in children_raw.split("|") if c.strip()]
                if children_raw
                else []
            )
            if isinstance(value, str) and "\n" in value:
                body_text = value
                body_type = "block"
            else:
                body_text = repr(value)
                body_type = "expr"
            # If this key was a template (leading $), allocate a unique
            # internal name so it doesn't collide with normal components.
            if tpl_level > 0:
                cname = f"__tpl_{len(comps)}"
            else:
                cname = name
            comps[cname] = Component(
                name=cname,
                children=children,
                body=body_text,
                body_type=body_type,
                source=source,
                orig_key=ks,
                template_level=tpl_level,
                base_name=name,
            )
    # Attempt to locate original key lines and extract the original
    # textual body for each component so runtime can report helpful
    # error messages with filename and line numbers.
    try:
        lines = yaml_text.splitlines()
        for name, comp in comps.items():
            if not comp.orig_key:
                continue
            pattern = r"^\s*" + re.escape(comp.orig_key) + r"\s*:(.*)$"
            found = None
            for i, ln in enumerate(lines):
                m = re.match(pattern, ln)
                if m:
                    found = (i, m.group(1))
                    break
            if not found:
                continue
            idx, rest = found
            comp.orig_start = idx + 1
            rest = rest or ""
            rest_s = rest.strip()
            if rest_s.startswith("|") or rest_s.startswith(">"):
                # collect following indented lines
                j = idx + 1
                body_lines = []
                while j < len(lines):
                    l = lines[j]
                    if l.strip() == "":
                        body_lines.append("")
                        j += 1
                        continue
                    if not l.startswith(" "):
                        break
                    body_lines.append(l)
                    j += 1
                # dedent
                comp.orig_body = textwrap.dedent("\n".join(body_lines)).lstrip("\n")
            elif rest_s != "":
                comp.orig_body = rest_s
            else:
                comp.orig_body = ""
    except Exception:
        # Best-effort only; don't fail parsing on extraction errors
        pass

    return comps
