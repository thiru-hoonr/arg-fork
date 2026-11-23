"""Module (tree-sitter) classes and edges for Scala. Same row contract as edgeref_scala.py; rules follow scalameta."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter scala: class/trait/object/enum definitions, extends_clause types, import_declaration, call_expression on name / a.b / f[T]"
CLS = {'class_definition': 'class', 'trait_definition': 'trait', 'object_definition': 'object', 'enum_definition': 'enum'}

def _simple(t):
    while t is not None and t.type not in ('type_identifier', 'identifier'):
        if t.type == 'generic_type': t = t.child_by_field_name('type')
        elif t.type in ('stable_type_identifier', 'projected_type'): t = t.named_children[-1]
        elif t.type == 'annotated_type': t = t.named_children[0]
        else: return None
    return t

def _callee(f):
    if f.type == 'generic_function': f = f.child_by_field_name('function')
    if f.type == 'identifier': return f
    if f.type == 'field_expression': return f.child_by_field_name('field')
    return None

def edges(root, files):
    p = get_parser('scala'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            L = n.start_point[0] + 1
            if n.type in CLS:
                nm = n.child_by_field_name('name')
                if nm is None: continue
                name = nm.text.decode()
                out.append(dict(e='class', file=rel, line=L, name=name, kind=CLS[n.type]))
                ex = n.child_by_field_name('extend')
                if ex is not None:
                    for ty in [x for x in ex.children_by_field_name('type') if x.is_named]:
                        s = _simple(ty)
                        out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=s.text.decode() if s is not None else ty.text.decode()))
            elif n.type == 'import_declaration':
                out.append(dict(e='import', file=rel, line=L, name=n.text.decode()[7:200]))
            elif n.type == 'call_expression':
                i = _callee(n.child_by_field_name('function'))
                if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=L, name=i.text.decode()))
    return out
