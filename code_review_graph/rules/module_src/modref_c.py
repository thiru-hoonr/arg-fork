"""Module (tree-sitter) classes and edges for C. Shared walker: modref_cfam.py."""
import modref_cfam, extract
from modref_cfam import edges_for
LABEL = "tree-sitter c (+c_prep): named struct/union/enum with body, #include lines, call_expression callee"
def edges(root, files): return edges_for(root, files, 'c', extract.c_prep)
