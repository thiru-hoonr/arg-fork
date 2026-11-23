"""Module (tree-sitter) classes and edges for C#. Same row contract as edgeref_csharp.py; rules follow Roslyn syntax."""
import re
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter c_sharp: class/struct/interface/record/enum declarations (line incl. attributes), base_list, using_directive, invocation_expression on name / a.b / a?.b / generic"
CLS = {'class_declaration': 'class', 'struct_declaration': 'struct', 'interface_declaration': 'interface', 'record_declaration': 'record',
       'record_struct_declaration': 'record', 'enum_declaration': 'enum'}

def _simple(t):
    t = re.split(r'[<(]', t, 1)[0].strip(); return t.rsplit('.', 1)[-1]

def _name(n):
    if n is None: return None
    if n.type == 'generic_name': n = n.named_children[0]
    return n if n.type == 'identifier' else None

def _callee(f):
    if f.type in ('identifier', 'generic_name'): return _name(f)
    if f.type == 'member_access_expression': return _name(f.child_by_field_name('name'))
    if f.type == 'conditional_access_expression':
        mb = f.named_children[-1]
        if mb.type == 'member_binding_expression': return _name(mb.child_by_field_name('name'))
    if f.type == 'member_binding_expression': return _name(f.child_by_field_name('name'))
    return None

def edges(root, files):
    p = get_parser('csharp'); out = []
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
                if n.type != 'enum_declaration':
                    for b in n.named_children:
                        if b.type == 'base_list':
                            for s in b.named_children:
                                if s.type == 'argument_list': continue
                                out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=_simple(s.text.decode())))
            elif n.type == 'using_directive':
                out.append(dict(e='import', file=rel, line=L, name=n.named_children[-1].text.decode() if n.named_children else ''))
            elif n.type == 'invocation_expression':
                i = _callee(n.child_by_field_name('function'))
                if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=L, name=i.text.decode()))
    return out
