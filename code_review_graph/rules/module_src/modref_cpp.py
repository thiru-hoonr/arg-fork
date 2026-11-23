"""Module (tree-sitter) classes and edges for C++. Shared walker: modref_cfam.py."""
import modref_cfam, extract
from modref_cfam import edges_for
LABEL = "tree-sitter cpp (+cpp_prep): named class/struct/union/enum with body, base_class_clause, #include, call_expression"
def edges(root, files): return edges_for(root, files, 'cpp', extract.cpp_prep)
