"""Module (tree-sitter) classes and edges for Objective-C. Shared walker: modref_cfam.py."""
import modref_cfam, extract
from modref_cfam import edges_for
LABEL = "tree-sitter objc (+objc_prep): @interface/@protocol + C records, superclass+protocols, #import/#include, calls+messages"
def edges(root, files): return edges_for(root, files, 'objc', extract.objc_prep)
