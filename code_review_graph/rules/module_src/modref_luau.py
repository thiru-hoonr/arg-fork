"""Module (tree-sitter) edges for Luau: same rules as modref_lua.py on the luau grammar; require(<any arg>) is an import
(full_moon reference takes the argument's source text)."""
import modref_lua as _L
LABEL = "tree-sitter luau: function_call (identifier / dot_index field / method_index method), require(<arg>) imports; calls only in chains ending in a call (full_moon FunctionCall), none inside ERROR"

def edges(root, files):
    return _L.edges(root, files, grammar='luau', any_arg=True, chain_rule=True)
