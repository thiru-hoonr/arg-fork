"""Module (tree-sitter) classes and edges for javascript: thin adapter over core-module/edges.py + classes.py."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core-module'))
import edges as _E, classes as _C
LANG = 'javascript'
LABEL = "tree-sitter javascript: core-module/classes.py (module_classes) + core-module/edges.py (module_edges)"

def edges(root, files):
    out = []
    for f in files:
        try:
            E = _E.module_edges(root, f, LANG)
            C = _C.module_classes(root, f, LANG)
        except Exception:
            continue
        out += [dict(e='class', file=f, line=c['line_start'], name=c['name'], kind=c['kind']) for c in C if c['name']]
        for r in E['inherits']:
            out.append(dict(e='inherits', file=f, line=r['line'], cls=r['cls'], sup=r['sup']))
        for r in E['import']:
            out.append(dict(e='import', file=f, line=r['line'], name=r['name']))
        for r in E['call']:
            out.append(dict(e='call', file=f, line=r['line'], start=r.get('start', r['line']), name=r['name']))
    return out
