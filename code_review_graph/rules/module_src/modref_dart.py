"""Module (tree-sitter) classes and edges for Dart. Same row contract as edgeref_dart.py; rules follow package:analyzer."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter dart: class/mixin/enum/extension type/class alias (line after annotations), superclass/mixins/interfaces/on, library_import, name + argument_part selectors, new/const"
CLS = {'class_definition': 'class', 'mixin_declaration': 'mixin', 'enum_declaration': 'enum', 'extension_type_declaration': 'extension type',
       'mixin_application_class': 'class alias'}

def _tnames(n):   # `p.Type`: the last identifier of a prefixed name
    return [c for c in n.named_children if c.type == 'type_identifier' and not (c.next_sibling is not None and c.next_sibling.type == '.')]

def _sups(n):
    out = []
    for c in n.named_children:
        if c.type == 'superclass':
            out += _tnames(c)
            for m in c.named_children:
                if m.type == 'mixins': out += _tnames(m)
        elif c.type in ('mixins', 'interfaces'): out += _tnames(c)
    if n.type == 'mixin_declaration':   # `mixin M on A, B`: type_identifiers directly under the declaration
        out += [c for c in n.named_children if c.type == 'type_identifier']
    if n.type == 'mixin_application_class':
        out += [c for c in n.named_children if c.type == 'type_identifier']
        for c in n.named_children:
            if c.type == 'mixin_application': out += [x for x in c.named_children if x.type == 'type_identifier'] + sum((_tnames(m) for m in c.named_children if m.type in ('mixins', 'interfaces')), [])
    return out

def _first_line(n):
    for c in n.children:
        if c.type not in ('annotation', 'comment', 'documentation_comment', 'metadata'): return c.start_point[0] + 1
    return n.start_point[0] + 1

def _sel_name(prev):
    if prev is None: return None
    if prev.type == 'identifier': return prev
    if prev.type in ('unconditional_assignable_selector', 'conditional_assignable_selector'):   # super.f()
        return prev.named_children[-1] if prev.named_children and prev.named_children[-1].type == 'identifier' else None
    if prev.type == 'selector':
        a = prev.named_children[0] if prev.named_children else None
        if a is not None and a.type in ('unconditional_assignable_selector', 'conditional_assignable_selector'):
            i = a.named_children[-1] if a.named_children else None
            if i is not None and i.type == 'identifier': return i
    return None

def edges(root, files):
    p = get_parser('dart'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            if n.type in CLS:
                nm = n.child_by_field_name('name') or next((c for c in n.named_children if c.type == 'identifier'), None)
                if nm is None: continue
                name, L = nm.text.decode(), _first_line(n)
                out.append(dict(e='class', file=rel, line=L, name=name, kind=CLS[n.type]))
                for s in _sups(n):
                    out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=s.text.decode()))
            elif n.type == 'library_import':
                out.append(dict(e='import', file=rel, line=_first_line(n.parent) if n.parent.type == 'import_or_export' else n.start_point[0] + 1, name=n.text.decode()))
            elif n.type == 'selector' and n.named_children and n.named_children[0].type == 'argument_part':
                i = _sel_name(n.prev_named_sibling)
                if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=i.start_point[0] + 1, name=i.text.decode()))
            elif n.type == 'cascade_section':
                k = n.named_children
                for a, b in zip(k, k[1:]):
                    if b.type == 'argument_part' and a.type == 'cascade_selector' and a.named_children and a.named_children[-1].type == 'identifier':
                        i = a.named_children[-1]; out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=i.start_point[0] + 1, name=i.text.decode()))
                    elif b.type == 'argument_part' and a.type in ('unconditional_assignable_selector', 'conditional_assignable_selector') and a.named_children:
                        i = a.named_children[-1]; out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=i.start_point[0] + 1, name=i.text.decode()))
                    elif b.type == 'argument_part' and a.type == 'selector':
                        i = _sel_name(a)
                        if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=i.start_point[0] + 1, name=i.text.decode()))
            elif n.type in ('new_expression', 'const_object_expression', 'constructor_invocation'):
                ids = [c for c in n.named_children if c.type in ('type_identifier', 'identifier')]
                if ids: out.append(dict(e='call', file=rel, line=ids[-1].start_point[0] + 1, start=n.start_point[0] + 1, name=ids[-1].text.decode()))
    return out
