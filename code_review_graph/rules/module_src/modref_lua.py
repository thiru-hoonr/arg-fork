"""Module (tree-sitter) edges for Lua. Same row contract/conventions as edgeref_lua.py (luaparse): call = function_call whose
name is an identifier / dot_index field / method_index method (last name); import = require with a literal string arg."""
from pathlib import Path
from tree_sitter_language_pack import get_parser
GRAMMAR = 'lua'
LABEL = "tree-sitter lua: function_call (identifier / dot_index field / method_index method), require(\"m\") imports"

def _name(n):
    if n is None: return None
    if n.type == 'identifier': return n
    if n.type == 'dot_index_expression': return n.child_by_field_name('field')
    if n.type == 'method_index_expression': return n.child_by_field_name('method')
    return None

def _arg(call, any_text):
    a = call.child_by_field_name('arguments')
    if a is None or not a.named_children: return None
    x = a.named_children[0]
    if x.type == 'string':
        return x.text.decode()[1:-1] if not x.text.startswith(b'[') else None
    return x.text.decode().strip() if any_text else None

IDX = ('dot_index_expression', 'method_index_expression', 'bracket_index_expression')

def _chain_ends_in_call(n):
    # full_moon visits a FunctionCall only when the whole suffix chain ends in a call; `f().x` is a Var, not visited
    while n.parent is not None and (n.parent.type in IDX and n.parent.child_by_field_name('table') == n
                                    or n.parent.type == 'function_call' and n.parent.child_by_field_name('name') == n):
        n = n.parent
    return n.type == 'function_call'

def _in_error(n):
    while n is not None:
        if n.type == 'ERROR': return True
        n = n.parent
    return False

def edges(root, files, grammar=GRAMMAR, any_arg=False, chain_rule=False):
    p = get_parser(grammar); out = []
    for rel in files:
        t = p.parse((Path(root) / rel).read_bytes())
        st = [t.root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            if n.type != 'function_call': continue
            if chain_rule and (not _chain_ends_in_call(n) or _in_error(n)): continue
            i = _name(n.child_by_field_name('name'))
            if i is None or i.type != 'identifier': continue
            nm = i.text.decode(); L = n.start_point[0] + 1
            out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=L, name=nm))
            if nm == 'require' and n.child_by_field_name('name').type == 'identifier':
                a = _arg(n, any_arg)
                if a is not None: out.append(dict(e='import', file=rel, line=L, name=a))
    return out
