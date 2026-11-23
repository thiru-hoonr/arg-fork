"""Module (tree-sitter) classes and edges for Elixir. Same row contract as edgeref_elixir.py (Code.string_to_quoted)."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter elixir: defmodule/defprotocol calls; @behaviour in module; alias/import/require/use; local+remote call nodes minus special forms/def heads"
SKIP = set("""def defp defmacro defmacrop defmodule defprotocol defimpl defstruct defexception defdelegate defguard defguardp
defoverridable alias import require use __aliases__ __block__ __MODULE__ __ENV__ __CALLER__ __DIR__ __STACKTRACE__
fn case cond if unless with for try receive quote unquote unquote_splicing super __cursor__""".split())
DEFS = {'def', 'defp', 'defmacro', 'defmacrop', 'defguard', 'defguardp'}

def _args(n):
    a = next((c for c in n.named_children if c.type == 'arguments'), None)
    return a.named_children if a is not None else []

def _last(n):
    t = n.text.decode()
    return t.lstrip(':').split('.')[-1]

def edges(root, files):
    p = get_parser('elixir'); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        if t.root_node.has_error: continue  # Code.string_to_quoted rejects unparsable files
        def walk(n, mod):
            if n.type == 'unary_operator' and n.child_by_field_name('operator') is not None and n.child_by_field_name('operator').type == '@':
                o = n.child_by_field_name('operand')
                if o is not None and o.type == 'call' and mod is not None:
                    tg = o.child_by_field_name('target'); a = _args(o)
                    if tg is not None and tg.text == b'behaviour' and len(a) == 1:
                        out.append(dict(e='inherits', file=rel, line=mod[1], cls=mod[0], sup=_last(a[0]))); return
            if n.type == 'call':
                tg = n.child_by_field_name('target'); a = _args(n); L = n.start_point[0] + 1
                if tg is not None and tg.type == 'identifier':
                    name = tg.text.decode()
                    if name in ('defmodule', 'defprotocol') and a:
                        nm = a[0].text.decode()
                        out.append(dict(e='class', file=rel, line=L, name=nm, kind='module' if name == 'defmodule' else 'protocol'))
                        for c in n.named_children:
                            if c.type == 'arguments':
                                for x in c.named_children[1:]: walk(x, (nm, L))
                            elif c != tg: walk(c, (nm, L))
                        return
                    if name in DEFS:
                        for c in n.named_children:
                            if c.type == 'arguments':
                                for x in c.named_children[1:]: walk(x, mod)
                            elif c != tg: walk(c, mod)
                        return
                    if name in ('alias', 'import', 'require', 'use'):
                        out.append(dict(e='import', file=rel, line=L, name=a[0].text.decode() if a else ''))
                    elif name not in SKIP:
                        out.append(dict(e='call', file=rel, line=tg.start_point[0] + 1, start=L, name=name))
                elif tg is not None and tg.type == 'dot':
                    r = tg.child_by_field_name('right')
                    has_args = any(c.type in ('arguments', 'do_block') for c in n.named_children) and \
                        (any(c.type == 'do_block' for c in n.named_children) or b'(' in n.text[len(tg.text):len(tg.text) + 2] or a)
                    if r is not None and r.type == 'identifier' and has_args:
                        dot = next((c for c in tg.children if c.type == '.'), tg)
                        out.append(dict(e='call', file=rel, line=dot.start_point[0] + 1, start=L, name=r.text.decode()))
            # desugared calls Code.string_to_quoted produces: "#{x}" -> Kernel.to_string, :"a#{x}" -> :erlang.binary_to_atom,
            # m[k] -> Access.get, alias A.{B, C} -> A.{}(B, C), 'a#{x}' -> List.to_charlist
            if n.type == 'interpolation':
                out.append(dict(e='call', file=rel, line=n.start_point[0] + 1, start=n.start_point[0] + 1, name='to_string'))
            elif n.type in ('quoted_atom', 'quoted_keyword') and any(c.type == 'interpolation' for c in n.named_children):
                out.append(dict(e='call', file=rel, line=n.start_point[0] + 1, start=n.start_point[0] + 1, name='binary_to_atom'))
            elif n.type == 'charlist' and any(c.type == 'interpolation' for c in n.named_children):
                out.append(dict(e='call', file=rel, line=n.start_point[0] + 1, start=n.start_point[0] + 1, name='to_charlist'))
            elif n.type == 'access_call':
                out.append(dict(e='call', file=rel, line=n.start_point[0] + 1, start=n.start_point[0] + 1, name='get'))
            elif n.type == 'dot' and n.child_by_field_name('right') is not None and n.child_by_field_name('right').type == 'tuple':
                dot = next((c for c in n.children if c.type == '.'), n)
                out.append(dict(e='call', file=rel, line=dot.start_point[0] + 1, start=n.start_point[0] + 1, name='{}'))
            for c in n.named_children: walk(c, mod)
        import sys; sys.setrecursionlimit(100000)
        walk(t.root_node, None)
    return out
