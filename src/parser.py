from dataclasses import dataclass
from typing import List, Dict
import re
import yaml


@dataclass
class Component:
    name: str
    children: List[str]
    body: str
    body_type: str  # 'expr' or 'block'


def parse_components(yaml_text: str) -> Dict[str, Component]:
    """Parse top-level components from YAML text.

    Each top-level key is a component signature `name(child|other)` or `name`.
    The value is the body (string or YAML scalar). Returns a mapping of name->Component.
    """
    loaded = yaml.safe_load(yaml_text)
    if not isinstance(loaded, dict):
        raise ValueError("YAML root must be a mapping of component_name: body")
    comps: Dict[str, Component] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise ValueError("component name must be a string")
        m = re.match(
            r"^(?P<name>[A-Za-z_]\w*)(?:\((?P<children>[^)]+)\))?$", key.strip()
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
        body_text = repr(value)
        body_type = "block" if "\n" in body_text else "expr"
        comps[name] = Component(
            name=name, children=children, body=body_text, body_type=body_type
        )
    return comps
