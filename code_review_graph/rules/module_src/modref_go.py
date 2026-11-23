"""Module (tree-sitter) classes and edges for Go. Same row contract as edgeref_go.py; rules follow what go/ast reports."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter go: type_spec (struct/interface/other), embedded fields, import_spec, call_expression"

def _last(n):
    while n is not None and n.type in ('pointer_type', 'generic_type', 'qualified_type', 'selector_expression', 'index_expression', 'parenthesized_expression'):
        if n.type == 'qualified_type': n = n.child_by_field_name('name')
        elif n.type == 'selector_expression': n = n.child_by_field_name('field')
        elif n.type == 'generic_type': n = n.child_by_field_name('type')
        elif n.type == 'index_expression': n = n.child_by_field_name('operand')
        else: n = n.named_children[0] if n.named_children else None
    return n if n is not None and n.type in ('identifier', 'type_identifier', 'field_identifier', 'package_identifier') else None

def edges(root, files):
    p = get_parser('go'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            L = n.start_point[0] + 1
            if n.type in ('type_spec', 'type_alias'):
                name = n.child_by_field_name('name'); ty = n.child_by_field_name('type')
                out.append(dict(e='class', file=rel, line=L, name=name.text.decode(), kind=ty.type if ty is not None else 'type'))
                if ty is not None and ty.type in ('struct_type', 'interface_type'):
                    for f in ty.named_children[0].named_children if ty.type == 'struct_type' and ty.named_children else ty.named_children:
                        if f.type == 'field_declaration' and f.child_by_field_name('name') is None:
                            i = _last(f.child_by_field_name('type'))
                            if i is not None: out.append(dict(e='inherits', file=rel, line=L, cls=name.text.decode(), sup=i.text.decode()))
                        elif f.type in ('type_elem', 'constraint_elem'):
                            for c in f.named_children:
                                i = _last(c)
                                if i is not None: out.append(dict(e='inherits', file=rel, line=L, cls=name.text.decode(), sup=i.text.decode()))
            elif n.type == 'import_spec':
                out.append(dict(e='import', file=rel, line=L, name=n.child_by_field_name('path').text.decode().strip('"`')))
            elif n.type == 'call_expression':
                i = _last(n.child_by_field_name('function'))
                if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=L, name=i.text.decode()))
    return out
