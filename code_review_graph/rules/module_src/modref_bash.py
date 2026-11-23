"""Module (tree-sitter) edges for Bash. Same row contract as edgeref_bash.py (mvdan/sh conventions)."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter bash: command with literal command_name as call; source/. as import"
NA = {"class": "Bash has no classes", "inherits": "Bash has no classes, so no inheritance"}

def edges(root, files):
    p = get_parser('bash'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            if n.type == 'test_command' and n.text.startswith(b'[') and not n.text.startswith(b'[['):
                out.append(dict(e='call', file=rel, line=n.start_point[0] + 1, name='[')); continue
            if n.type == 'unset_command':
                out.append(dict(e='call', file=rel, line=n.start_point[0] + 1, name=n.children[0].text.decode())); continue
            if n.type != 'command' or n.parent.type == 'ERROR' or n.has_error: continue
            cn = n.child_by_field_name('name')
            if cn is None or len(cn.named_children) != 1 or cn.named_children[0].type not in ('word',): continue
            name = cn.text.decode(); L = cn.start_point[0] + 1
            if name in ('source', '.'):
                out.append(dict(e='import', file=rel, line=L))
            out.append(dict(e='call', file=rel, line=L, name=name))
    return out
