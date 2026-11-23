"""Module (tree-sitter) edges for Nix. Same row contract as edgeref_nix.py (rnix-parser). Classes/inherits are NA."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter nix: innermost apply_expression of a curried chain, callee variable / last attr of select; import applications"

def edges(root, files):
    p = get_parser('nix'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        if t.root_node.has_error: continue  # reference skips files rnix reports errors for
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            if n.type != 'apply_expression': continue
            f = n.child_by_field_name('function')
            if f.type == 'apply_expression': continue  # only the innermost application
            top = n
            while top.parent is not None and top.parent.type == 'apply_expression' and top.parent.child_by_field_name('function') == top: top = top.parent
            if f.type == 'variable_expression': c = f.child_by_field_name('name')
            elif f.type == 'select_expression':
                ap = f.child_by_field_name('attrpath'); c = ap.named_children[-1] if ap is not None and ap.named_children else None
            else: c = None
            if c is None: continue
            name = c.text.decode().strip('"'); start = top.start_point[0] + 1
            if name == 'import' and (f.type == 'variable_expression' or f.text == b'builtins.import'):
                out.append(dict(e='import', file=rel, line=start, name='import'))
            out.append(dict(e='call', file=rel, line=c.start_point[0] + 1, start=start, name=name))
    return out
