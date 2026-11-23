"""Module (tree-sitter) classes and edges for Julia. Same row contract as edgeref_julia.py (JuliaSyntax)."""
import sys
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter julia: struct/abstract/primitive definitions, <: ; using/import statements; call/broadcast call nodes minus signatures"
KIND = {'struct_definition': 'struct', 'abstract_definition': 'abstract', 'primitive_definition': 'primitive'}

def _name(n):
    """(name, token) of a callee / type expression: last name of a.b, head of A{T}, inside parens."""
    while n is not None:
        if n.type in ('identifier', 'operator'): return n.text.decode(), n
        if n.type == 'field_expression': n = n.named_children[-1]
        elif n.type in ('parametrized_type_expression', 'parenthesized_expression', 'typed_expression', 'quote_expression'):
            n = n.named_children[0] if n.type != 'typed_expression' else n.named_children[-1]
        else: return None, None
    return None, None

def _tname(n):
    while n is not None and n.type in ('binary_expression', 'parametrized_type_expression'): n = n.named_children[0]
    return n.text.decode() if n is not None and n.type == 'identifier' else None

def edges(root, files):
    p = get_parser('julia'); out = []; sys.setrecursionlimit(100000)
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        def walk(n):
            ty = n.type
            if ty in KIND:
                th = next((c for c in n.named_children if c.type == 'type_head'), None)
                sig = th.named_children[0] if th is not None and th.named_children else None
                nm = _tname(sig)
                if nm is not None:
                    L = n.start_point[0] + 1
                    out.append(dict(e='class', file=rel, line=L, name=nm, kind=KIND[ty]))
                    if sig.type == 'binary_expression' and len(sig.named_children) == 3 and sig.named_children[1].text == b'<:':
                        s, _ = _name(sig.named_children[2])
                        if s is not None: out.append(dict(e='inherits', file=rel, line=L, cls=nm, sup=s))
                for c in n.named_children:
                    if c is not th: walk(c)
                return
            if ty in ('using_statement', 'import_statement'):
                out.append(dict(e='import', file=rel, line=n.start_point[0] + 1, name=ty.split('_')[0])); return
            if ty in ('function_definition', 'macro_definition'):
                for c in n.named_children:
                    if c.type != 'signature': walk(c)
                return
            if ty == 'assignment' and len(n.named_children) == 3 and n.named_children[1].text == b'=' and \
                    n.named_children[0].type in ('call_expression', 'where_expression'):
                walk(n.named_children[2]); return
            if ty == 'macrocall_expression':
                for c in n.named_children:
                    if c.type != 'macro_identifier': walk(c)
                return
            if ty in ('call_expression', 'broadcast_call_expression') and n.named_children:
                callee = n.named_children[0]
                al = next((c for c in n.named_children if c.type == 'argument_list'), None)
                prefix_op = callee.type == 'operator' and al is not None and len(al.named_children) == 1   # -(x), !(x): prefix operator call
                if callee.type not in ('call_expression', 'broadcast_call_expression') and not prefix_op:
                    nm, tok = _name(callee)
                    if nm is not None:
                        out.append(dict(e='call', file=rel, line=tok.start_point[0] + 1, start=n.start_point[0] + 1, name=nm))
            if ty == 'binary_expression' and len(n.named_children) == 3 and n.named_children[1].text == b'isa' \
                    and n.named_children[2].type == 'tuple_expression' and n.named_children[2].start_byte == n.named_children[1].end_byte:
                o = n.named_children[1]   # `isa(a, B)` after a macro name: tree-sitter reads isa as the infix operator
                out.append(dict(e='call', file=rel, line=o.start_point[0] + 1, start=o.start_point[0] + 1, name='isa'))
            for c in n.named_children: walk(c)
        walk(t.root_node)
    return out
