"""Module (tree-sitter python) classes and edges for Jupyter notebooks. Cells selected/joined/numbered as edgeref_notebook.py
(python/r kernel, %python/%sql/%r/%scala/%md/%sh magics, %/! lines dropped, cells joined with one newline); each cell parsed
on its own and a cell with a parse ERROR is skipped (CPython ast skips a cell with a SyntaxError).
class = class_definition (line of `class`), inherits = each base (last name), import = import / from-import, call = call (last name)."""
import json
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter python over code cells (ref numbering): class_definition, bases, import/from, call"
MAGIC = {"%python": "python", "%sql": "sql", "%r": "r"}
SKIP = ("%scala", "%md", "%sh")

def chunks_of(path):
    nb = json.loads(path.read_text(encoding="utf-8")); md = nb.get("metadata", {})
    kernel = (md.get("kernelspec", {}).get("language") or md.get("language_info", {}).get("name") or "python").lower()
    if kernel not in ("python", "r"): return []
    out = []
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code": continue
        lines = cell.get("source", [])
        if isinstance(lines, str): lines = lines.splitlines(keepends=True)
        if not lines: continue
        first = lines[0].strip(); lang = kernel; body = lines
        for m, l in MAGIC.items():
            if first == m or first.startswith(m + " "): lang, body = l, lines[1:]; break
        else:
            if any(first == s or first.startswith(s + " ") for s in SKIP): body = []
        body = [ln for ln in body if not ln.lstrip().startswith(("%", "!"))]
        if body: out.append((lang, "".join(body)))
    return [c[1] for c in out if c[0] == "python"]

def _last(f):
    if f is None: return None
    if f.type == 'identifier': return f
    if f.type == 'attribute': return f.child_by_field_name('attribute')
    return None

def edges(root, files):
    p = get_parser('python'); out = []
    for rel in files:
        try: srcs = chunks_of(Path(root) / rel)
        except Exception: continue
        running = 1
        for src in srcs:
            off = running - 1; running += src.count("\n") + 1
            t = p.parse(src.encode())
            if t.root_node.has_error: continue
            st = [t.root_node]
            while st:
                n = st.pop(); st.extend(n.children); L = n.start_point[0] + 1 + off
                if n.type == 'class_definition':
                    nm = n.child_by_field_name('name').text.decode()
                    out.append(dict(e='class', file=rel, line=L, name=nm, kind='class'))
                    sc = n.child_by_field_name('superclasses')
                    for b in (sc.named_children if sc is not None else []):
                        if b.type == 'keyword_argument': continue
                        i = _last(b); out.append(dict(e='inherits', file=rel, line=L, cls=nm, sup=i.text.decode() if i is not None else b.text.decode()))
                elif n.type == 'import_statement':
                    for a in n.named_children:
                        x = a.child_by_field_name('name') if a.type == 'aliased_import' else a
                        out.append(dict(e='import', file=rel, line=L, name=x.text.decode()))
                elif n.type == 'import_from_statement':
                    m = n.child_by_field_name('module_name')
                    out.append(dict(e='import', file=rel, line=L, name=m.text.decode().replace(' ', '') if m is not None else ''))
                elif n.type == 'call':
                    i = _last(n.child_by_field_name('function'))
                    if i is not None:
                        out.append(dict(e='call', file=rel, line=i.end_point[0] + 1 + off, start=L, name=i.text.decode()))
    return out
