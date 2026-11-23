"""Module (tree-sitter) edges for R. Same row contract as edgeref_r.py (getParseData conventions)."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter r: call with identifier / pkg::f function as call; library/require/source as import"
NA = {"class": "R's grammar has no class declaration", "inherits": "no class declaration in R's grammar"}

def edges(root, files):
    p = get_parser('r'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            if n.type != 'call': continue
            f = n.child_by_field_name('function')
            if f is not None and f.type == 'namespace_operator':
                f = f.child_by_field_name('rhs') or f.named_children[-1]
            if f is None or f.type not in ('identifier', 'string', 'return'): continue
            name = f.text.decode().strip('`'); L = f.start_point[0] + 1
            if name in ('library', 'require', 'source') and n.child_by_field_name('function').type == 'identifier':
                out.append(dict(e='import', file=rel, line=L))
            out.append(dict(e='call', file=rel, line=L, name=name))
    return out
