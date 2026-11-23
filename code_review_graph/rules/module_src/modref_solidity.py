"""Module (tree-sitter) classes and edges for Solidity. Same row contract as edgeref_solidity.py (solc parse-stage AST)."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter solidity: contract/interface/library declarations, inheritance_specifier; import_directive; call_expression/emit/revert with identifier or member callee"
KIND = {'contract_declaration': 'contract', 'interface_declaration': 'interface', 'library_declaration': 'library'}

def _callee(e):
    while e is not None and e.type in ('expression', 'parenthesized_expression') and e.named_child_count == 1: e = e.named_children[0]
    if e is None: return None
    if e.type == 'identifier': return e
    if e.type == 'member_expression': return e.child_by_field_name('property')
    return None

def edges(root, files):
    p = get_parser('solidity'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children); L = n.start_point[0] + 1
            if n.type in KIND:
                nm = n.child_by_field_name('name').text.decode()
                out.append(dict(e='class', file=rel, line=L, name=nm, kind=KIND[n.type]))
                for b in n.named_children:
                    if b.type == 'inheritance_specifier':
                        a = b.child_by_field_name('ancestor')
                        ids = [c for c in a.named_children if c.type == 'identifier'] if a is not None else []
                        if ids: out.append(dict(e='inherits', file=rel, line=L, cls=nm, sup=ids[-1].text.decode()))
            elif n.type == 'import_directive':
                out.append(dict(e='import', file=rel, line=L, name=''))
            elif n.type == 'call_expression':
                c = _callee(n.child_by_field_name('function'))
                if c is not None: out.append(dict(e='call', file=rel, line=c.start_point[0] + 1, start=L, name=c.text.decode()))
            elif n.type == 'meta_type_expression':   # type(X) is a FunctionCall to the `type` identifier in solc
                out.append(dict(e='call', file=rel, line=L, start=L, name='type'))
            elif n.type == 'emit_statement':
                c = _callee(n.child_by_field_name('name'))
                if c is not None: out.append(dict(e='call', file=rel, line=c.start_point[0] + 1, start=c.start_point[0] + 1, name=c.text.decode()))
            elif n.type == 'revert_statement':
                e = n.child_by_field_name('error')
                if e is not None and not any(c.type == 'revert_arguments' for c in n.named_children):
                    out.append(dict(e='call', file=rel, line=L, start=L, name='revert'))   # revert("msg"): builtin revert call
                elif e is not None:
                    c = _callee(e)
                    if c is not None: out.append(dict(e='call', file=rel, line=c.start_point[0] + 1, start=c.start_point[0] + 1, name=c.text.decode()))
                else:   # revert(...) is a call to the builtin revert
                    out.append(dict(e='call', file=rel, line=L, start=L, name='revert'))
    return out
