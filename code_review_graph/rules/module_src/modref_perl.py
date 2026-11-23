"""Module (tree-sitter) classes and edges for Perl. Same row contract as edgeref_perl.py (PPI conventions)."""
import re
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter perl: package_statement, use parent/base, use/require, function/ambiguous/method calls"

def edges(root, files):
    p = get_parser('perl'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]; cur = 'main'
        nodes = []
        while st:
            n = st.pop(); st.extend(reversed(n.children)); nodes.append(n)
        for n in nodes:
            L = n.start_point[0] + 1
            if n.type == 'package_statement':
                nm = n.child_by_field_name('name') or next((c for c in n.named_children if c.type == 'package'), None)
                if nm is None: continue
                cur = nm.text.decode()
                out.append(dict(e='class', file=rel, line=L, name=cur))
            elif n.type == 'use_statement':
                kids = [c for c in n.children]
                if kids and kids[0].type == 'no': continue
                out.append(dict(e='import', file=rel, line=L))
                mod = next((c for c in n.named_children if c.type == 'package'), None)
                if mod is not None and mod.text.decode() in ('parent', 'base'):
                    rest = n.text.decode()[mod.end_byte - n.start_byte:]
                    for s in re.findall(r"""['"]([\w:]+)['"]|qw\s*.([^)\]}>/|]*)|(?<![\w:-])([A-Z][\w:]*)""", rest):
                        for name in (s[0] or s[2] or '').split() + s[1].split():
                            if name != '-norequire':
                                out.append(dict(e='inherits', file=rel, line=L, cls=cur.split('::')[-1], sup=name.split('::')[-1]))
            elif n.type == 'require_expression':
                out.append(dict(e='import', file=rel, line=L))
            elif n.type in ('function_call_expression', 'ambiguous_function_call_expression', 'method_call_expression'):
                f = n.child_by_field_name('function') or n.child_by_field_name('method')
                if f is None:
                    f = next((c for c in n.children if c.type in ('function', 'method')), None)
                if f is None or f.type not in ('function', 'method', 'bareword', 'identifier'): continue
                out.append(dict(e='call', file=rel, line=f.start_point[0] + 1, name=f.text.decode()))
    return out
