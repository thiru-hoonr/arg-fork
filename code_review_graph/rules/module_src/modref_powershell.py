"""Module (tree-sitter) classes and edges for PowerShell. Same row contract as edgeref_powershell.py (Language.Parser conventions)."""
import sys
from pathlib import Path
from tree_sitter_language_pack import get_parser
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core-module'))
from extract import powershell_prep
LABEL = "tree-sitter powershell (+module prep): class/enum statements, base list, using module/dot-source/Import-Module, command + invokation_expression"

def edges(root, files):
    p = get_parser('powershell'); out = []
    for rel in files:
        t = p.parse(powershell_prep((Path(root) / rel).read_bytes()))
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            L = n.start_point[0] + 1
            if n.type in ('class_statement', 'enum_statement'):
                names = [c for c in n.children if c.type == 'simple_name']
                if not names: continue
                out.append(dict(e='class', file=rel, line=L, name=names[0].text.decode()))
                if n.type == 'class_statement':
                    for s in names[1:]:
                        out.append(dict(e='inherits', file=rel, line=L, cls=names[0].text.decode(), sup=s.text.decode()))
            elif n.type == 'command':
                op = next((c for c in n.children if c.type == 'command_invokation_operator'), None)
                cn = next((c for c in n.named_children if c.type in ('command_name', 'command_name_expr')), None)
                if op is not None and op.text.decode() == '.':
                    out.append(dict(e='import', file=rel, line=L)); continue
                fe = next((c for c in n.named_children if c.type == 'foreach_command'), None)
                if cn is None and fe is not None:
                    out.append(dict(e='call', file=rel, line=L, name=fe.text.decode().split()[0])); continue
                if cn is None: continue
                if cn.type == 'command_name_expr':
                    cn = next((c for c in cn.named_children if c.type == 'command_name'), None)
                    if cn is None: continue
                name = cn.text.decode()
                if name[:1].isdigit(): continue
                if name.lower() == 'using':
                    if 'module' in n.text.decode().lower().split()[1:2]: out.append(dict(e='import', file=rel, line=L))
                    continue
                if name.lower() == 'import-module': out.append(dict(e='import', file=rel, line=L))
                out.append(dict(e='call', file=rel, line=L, name=name))
            elif n.type == 'invokation_expression':
                m = next((c for c in n.named_children if c.type == 'member_name'), None)
                if m is not None and any(c.type == 'argument_list' for c in n.children):
                    out.append(dict(e='call', file=rel, line=m.start_point[0] + 1, name=m.text.decode()))
    return out
