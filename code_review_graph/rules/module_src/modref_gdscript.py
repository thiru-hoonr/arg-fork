"""Module (tree-sitter) classes and edges for GDScript. Same conventions as edgeref_gdscript.py (gdtoolkit):
class = class_name statement (file class) + inner class_definition; inherits = extends (identifier or "res://.../x.gd" -> x);
import = preload("literal"); call = call(identifier) and attribute_call (method name)."""
import os
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter gdscript: class_name_statement + class_definition, extends_statement, preload(\"...\") imports, call / attribute_call"

def _sup(ext):
    for c in ext.named_children:
        if c.type == 'string':
            return os.path.splitext(os.path.basename(c.text.decode().strip('"\'')))[0]
        ids = [x for x in _walk(c) if x.type in ('identifier', 'name', 'string')]
        if ids:
            v = ids[0].text.decode()
            return os.path.splitext(os.path.basename(v.strip('"\'')))[0] if ids[0].type == 'string' else v
    return None

def _walk(n):
    st = [n]
    while st:
        x = st.pop(); yield x; st.extend(reversed(x.children))

def edges(root, files):
    p = get_parser('gdscript'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes()); top = t.root_node.named_children
        cn = [c for c in top if c.type == 'class_name_statement']
        if cn:
            name = cn[0].child_by_field_name('name').text.decode(); L = cn[0].start_point[0] + 1
            out.append(dict(e='class', file=rel, line=L, name=name, kind='class_name'))
            for c in top:
                if c.type == 'extends_statement':
                    s = _sup(c)
                    if s: out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=s))
            # `class_name X extends Y` on one line: extends nested in class_name_statement
            for c in cn[0].named_children:
                if c.type == 'extends_statement':
                    s = _sup(c)
                    if s: out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=s))
        for n in _walk(t.root_node):
            L = n.start_point[0] + 1
            if n.type == 'class_definition':
                name = n.child_by_field_name('name').text.decode()
                out.append(dict(e='class', file=rel, line=L, name=name, kind='class'))
                ext = n.child_by_field_name('extends')
                if ext is None:   # `class X:` + `extends Y` as the first body line
                    b = n.child_by_field_name('body')
                    ext = next((c for c in (b.named_children if b is not None else []) if c.type == 'extends_statement'), None)
                if ext is not None:
                    s = _sup(ext)
                    if s: out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=s))
            elif n.type == 'call':
                f = n.named_children[0] if n.named_children else None
                if f is not None and f.type == 'identifier':
                    nm = f.text.decode(); out.append(dict(e='call', file=rel, line=L, start=L, name=nm))
                    a = n.child_by_field_name('arguments')
                    if nm == 'preload' and a is not None and a.named_children and a.named_children[0].type == 'string':
                        out.append(dict(e='import', file=rel, line=L, name=a.named_children[0].text.decode().strip('"\'')))
            elif n.type == 'attribute_call':
                f = n.named_children[0] if n.named_children else None
                if f is not None and f.type == 'identifier':
                    if f.text in (b'get', b'set'):
                        # get/set are keyword tokens in gdtoolkit's grammar, not NAMEs: the reference takes the previous NAME
                        prev = [x for x in n.parent.named_children[:n.parent.named_children.index(n)] if x.type == 'identifier'] \
                            if n.parent is not None and n.parent.type == 'attribute' else []
                        if not prev: continue
                        f = prev[-1]
                    out.append(dict(e='call', file=rel, line=f.start_point[0] + 1, start=L, name=f.text.decode()))
    return out
