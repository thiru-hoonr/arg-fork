"""Module (tree-sitter) classes and edges for PHP. Same row contract as edgeref_php.py (nikic/php-parser conventions)."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter php: class/interface/trait/enum, base_clause+class_interface_clause, namespace_use + include/require, call expressions (new excluded)"
CLS = ('class_declaration', 'interface_declaration', 'trait_declaration', 'enum_declaration')
INC = ('include_expression', 'include_once_expression', 'require_expression', 'require_once_expression')
CALLS = {'function_call_expression': 'function', 'member_call_expression': 'name',
         'nullsafe_member_call_expression': 'name', 'scoped_call_expression': 'name'}

def _last(n):
    if n is None: return None
    if n.type == 'qualified_name':
        n = n.child_by_field_name('name') or n.named_children[-1]
    return n if n.type == 'name' else None

def edges(root, files):
    p = get_parser('php'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            L = n.start_point[0] + 1
            if n.type in CLS:
                nm = n.child_by_field_name('name')
                if nm is None: continue
                out.append(dict(e='class', file=rel, line=L, name=nm.text.decode()))
                for c in n.children:
                    if c.type in ('base_clause', 'class_interface_clause'):
                        for s in c.named_children:
                            s = _last(s)
                            if s is not None: out.append(dict(e='inherits', file=rel, line=L, cls=nm.text.decode(), sup=s.text.decode()))
            elif n.type == 'namespace_use_declaration' or n.type in INC:
                out.append(dict(e='import', file=rel, line=L))
            elif n.type in CALLS:
                f = _last(n.child_by_field_name(CALLS[n.type]))
                if f is not None and not (n.type == 'function_call_expression' and f.text.decode().lower() in ('isset', 'empty', 'eval', 'exit', 'die', 'list', 'unset')): out.append(dict(e='call', file=rel, line=L, name=f.text.decode()))
    return out
