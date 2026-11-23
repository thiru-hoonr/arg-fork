"""Module (tree-sitter) classes and edges for Rust. Same row contract as edgeref_rust.py (syn)."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter rust: struct/enum/trait/union items; impl Trait for T + trait bounds; use/extern crate; call_expression path + method"
CLS = {'struct_item': 'struct', 'enum_item': 'enum', 'trait_item': 'trait', 'union_item': 'union'}

def _start(n):
    # syn spans an item from its first outer attribute
    p = n.prev_named_sibling; L = n.start_point[0] + 1
    while p is not None and p.type in ('attribute_item', 'line_comment', 'block_comment'):
        if p.type == 'attribute_item': L = p.start_point[0] + 1
        elif p.type == 'line_comment' and p.text.startswith(b'///'): L = p.start_point[0] + 1
        p = p.prev_named_sibling
    return L

def _last(n):
    while n is not None:
        if n.type in ('scoped_type_identifier', 'scoped_identifier'): n = n.child_by_field_name('name')
        elif n.type == 'generic_type': n = n.child_by_field_name('type')
        elif n.type == 'generic_function': n = n.child_by_field_name('function')
        elif n.type in ('reference_type', 'pointer_type'): n = n.child_by_field_name('type')
        else: break
    return n if n is not None and n.type in ('identifier', 'type_identifier', 'primitive_type') else None

def edges(root, files):
    p = get_parser('rust'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop()
            if n.type in ('macro_invocation', 'macro_definition'): continue
            st.extend(n.children)
            if n.type in CLS:
                nm = n.child_by_field_name('name'); L = _start(n)
                out.append(dict(e='class', file=rel, line=L, name=nm.text.decode(), kind=CLS[n.type]))
                if n.type == 'trait_item':
                    b = n.child_by_field_name('bounds')
                    if b is not None:
                        for c in b.named_children:
                            s = _last(c)
                            if s is not None: out.append(dict(e='inherits', file=rel, line=L, cls=nm.text.decode(), sup=s.text.decode()))
            elif n.type == 'impl_item' and n.child_by_field_name('trait') is not None:
                c = _last(n.child_by_field_name('type')); s = _last(n.child_by_field_name('trait'))
                if c is not None and s is not None:
                    out.append(dict(e='inherits', file=rel, line=_start(n), cls=c.text.decode(), sup=s.text.decode()))
            elif n.type in ('use_declaration', 'extern_crate_declaration'):
                out.append(dict(e='import', file=rel, line=_start(n), name=n.text.decode()))
            elif n.type == 'call_expression':
                f = n.child_by_field_name('function')
                if f is not None and f.type == 'generic_function': f = f.child_by_field_name('function')
                if f is not None and f.type == 'field_expression':
                    i = f.child_by_field_name('field')
                    if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=n.start_point[0] + 1, name=i.text.decode()))
                else:
                    i = _last(f)
                    if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=n.start_point[0] + 1, name=i.text.decode()))
    return out
