"""Module (tree-sitter) classes and edges for Kotlin. Same row contract as edgeref_kotlin.py; rules follow Kotlin PSI."""
import re
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter kotlin: class/object/companion declarations (named), delegation_specifier, import_header, call_expression with simple-name callee"

def _simple(t):
    t = re.sub(r'<.*', '', t, flags=re.S); t = re.sub(r'\(.*', '', t, flags=re.S).replace('?', '').strip()
    return t.rsplit('.', 1)[-1]

def _callee(f):
    if f.type == 'simple_identifier': return f
    if f.type == 'navigation_expression':
        s = f.named_children[-1]
        if s.type == 'navigation_suffix':
            i = s.named_children[-1] if s.named_children else None
            if i is not None and i.type == 'simple_identifier': return i
    return None

def edges(root, files):
    p = get_parser('kotlin'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes().replace(b'\r\n', b'\n'))
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            L = n.start_point[0] + 1
            if n.type in ('class_declaration', 'object_declaration', 'companion_object'):
                nm = next((c for c in n.named_children if c.type in ('type_identifier', 'simple_identifier')), None)
                if nm is None: continue
                name = nm.text.decode()
                out.append(dict(e='class', file=rel, line=L, name=name, kind=n.type))
                for d in n.named_children:
                    if d.type == 'delegation_specifiers': ds = d.named_children
                    elif d.type == 'delegation_specifier': ds = [d]
                    else: continue
                    for s in ds:
                        if s.type != 'delegation_specifier': continue
                        c = s.named_children[0] if s.named_children else None
                        if c is None: continue
                        if c.type in ('constructor_invocation', 'explicit_delegation'): c = c.named_children[0]
                        out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=_simple(c.text.decode())))
            elif n.type == 'import_header':
                out.append(dict(e='import', file=rel, line=L, name=n.named_children[0].text.decode() if n.named_children else ''))
            elif n.type == 'call_expression':
                i = _callee(n.named_children[0])
                if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=L, name=i.text.decode()))
    return out
