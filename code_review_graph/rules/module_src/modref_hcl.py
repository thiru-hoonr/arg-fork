"""Module (tree-sitter) edges for HCL. Same conventions as edgeref_hcl.py (hclsyntax): import = top-level `module` block
(name = literal source string, line = block line); call = every function_call (name = text before `(`, incl. provider::ns::f).
Files tree-sitter cannot parse cleanly are skipped (hclsyntax skips files with errors)."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter hcl: top-level module blocks (source) as imports, function_call; files with ERROR skipped"

def edges(root, files):
    p = get_parser('hcl'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        if t.root_node.has_error: continue
        body = next((c for c in t.root_node.named_children if c.type == 'body'), None)
        for b in (body.named_children if body is not None else []):
            if b.type == 'block' and b.named_children and b.named_children[0].text == b'module':
                name = ''
                inner = next((c for c in b.named_children if c.type == 'body'), None)
                for a in (inner.named_children if inner is not None else []):
                    if a.type == 'attribute' and a.named_children[0].text == b'source':
                        s = a.named_children[1]
                        lit = s.named_children[0] if s.named_children else None
                        if lit is not None and lit.type == 'literal_value' and lit.named_children[0].type == 'string_lit':
                            name = ''.join(x.text.decode() for x in lit.named_children[0].named_children if x.type == 'template_literal')
                out.append(dict(e='import', file=rel, line=b.start_point[0] + 1, name=name))
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            if n.type == 'function_call':
                L = n.start_point[0] + 1
                nm = n.text.decode().split('(', 1)[0].strip()
                out.append(dict(e='call', file=rel, line=L, start=L, name=nm))
    return out
