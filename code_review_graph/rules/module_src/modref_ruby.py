"""Module (tree-sitter) classes and edges for Ruby. Same row contract as edgeref_ruby.py (MRI AST conventions)."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter ruby: class/module (+singleton excluded), superclass, require/require_relative/load, call + bare-identifier command"
IMPORTS = {'require', 'require_relative', 'load'}

def _last(n):
    while n is not None and n.type == 'scope_resolution':
        n = n.child_by_field_name('name')
    return n

def edges(root, files):
    p = get_parser('ruby'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            L = n.start_point[0] + 1
            if n.type in ('class', 'module'):
                nm = _last(n.child_by_field_name('name'))
                if nm is None: continue
                out.append(dict(e='class', file=rel, line=L, name=nm.text.decode()))
                sc = n.child_by_field_name('superclass')
                if sc is not None and sc.named_children:
                    s = _last(sc.named_children[0])
                    if s is not None and s.type in ('constant', 'identifier'):
                        out.append(dict(e='inherits', file=rel, line=L, cls=nm.text.decode(), sup=s.text.decode()))
            elif n.type == 'call':
                m = n.child_by_field_name('method')
                if m is None or m.type not in ('identifier', 'constant'): continue
                name = m.text.decode()
                if n.child_by_field_name('receiver') is None and name in IMPORTS:
                    out.append(dict(e='import', file=rel, line=L, name=name))
                out.append(dict(e='call', file=rel, line=m.start_point[0] + 1, name=name))
            elif n.type == 'identifier' and n.parent is not None and n.parent.type in ('body_statement', 'program', 'then', 'else', 'do', 'block_body', 'begin', 'ensure', 'parenthesized_statements'):
                out.append(dict(e='call', file=rel, line=L, name=n.text.decode()))
    return out
