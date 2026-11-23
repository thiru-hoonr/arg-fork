"""Module (tree-sitter) classes and edges for Zig. Same row contract as edgeref_zig.py (std.zig.Ast). No inheritance in Zig."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter zig: VarDecl whose init is a struct/enum/union ContainerDecl; @import BUILTINIDENTIFIER; FnCallArguments callee identifier"

def _container(v):
    # VarDecl -> init expression that is exactly a ContainerDecl (through the ErrorUnionExpr/SuffixExpr wrappers)
    kids = v.children; eq = next((i for i, c in enumerate(kids) if c.type == '='), None)
    for c in (kids[eq + 1:eq + 2] if eq is not None else []):
        if c.type in ('ErrorUnionExpr', 'SuffixExpr'):
            n = c
            while n is not None and n.type in ('ErrorUnionExpr', 'SuffixExpr') and n.named_child_count == 1:
                n = n.named_children[0]
            if n is not None and n.type == 'ContainerDecl': return n
    return None

def edges(root, files):
    p = get_parser('zig'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            if n.type == 'VarDecl':
                cd = _container(n)
                if cd is None: continue
                kt = next((c for c in cd.named_children if c.type == 'ContainerDeclType'), None)
                kw = kt.text.decode().split('(')[0].strip() if kt is not None else ''
                if kw not in ('struct', 'enum', 'union'): continue
                name = next(c for c in n.named_children if c.type == 'IDENTIFIER')
                d = n.parent if n.parent is not None and n.parent.type in ('Decl', 'TopLevelDecl') else n
                out.append(dict(e='class', file=rel, line=d.start_point[0] + 1, name=name.text.decode(), kind=kw))
            elif n.type == 'BUILTINIDENTIFIER' and n.text == b'@import':
                a = n.next_named_sibling
                s = a.text.decode().strip('()').strip() if a is not None else ''
                out.append(dict(e='import', file=rel, line=n.start_point[0] + 1, name=s))
            elif n.type == 'FnCallArguments':
                c = n.prev_sibling
                if c is not None and c.type == 'IDENTIFIER':
                    s = n.parent
                    while s.parent is not None and s.type != 'SuffixExpr': s = s.parent
                    out.append(dict(e='call', file=rel, line=c.start_point[0] + 1, start=s.start_point[0] + 1, name=c.text.decode()))
    return out
