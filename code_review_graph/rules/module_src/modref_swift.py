"""Module (tree-sitter) classes and imports for Swift. Same rows as edgeref_swift.py (swiftc -dump-parse):
class/struct/enum/actor declarations (not extensions), line = start of the declaration (attributes/modifiers included,
as swiftc's range); import_declaration. inherits/call are N/A in the reference.
#if handling (line-preserving prep, as swiftc's parse dump shows it): only the first clause of an #if is parsed, later
clauses and directive lines are blanked; type declarations and imports inside an #if clause are not reported (the parse dump prints
them without the decl range line). An import's line is its `import` keyword (attributes on earlier lines excluded).
Protocols: the reference regex lists `protocol_decl`, but swiftc prints `(protocol ...`, so the reference has no protocol
rows; the module follows that (protocols not reported)."""
import re
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter swift (+#if first-clause prep): class_declaration (class/struct/enum/actor, not extension, not inside #if), import_declaration"
DIR = re.compile(r'^\s*#(if|elseif|else|endif)\b')

def _prep(src):
    lines = src.split('\n'); depth, dead, inif = 0, [], []   # dead: stack of "in a later clause" flags
    keep, inside = [], []
    for l in lines:
        m = DIR.match(l)
        if m:
            k = m.group(1)
            if k == 'if': dead.append(False)
            elif k in ('elseif', 'else') and dead: dead[-1] = True
            elif k == 'endif' and dead: dead.pop()
            keep.append(''); inside.append(True); continue
        keep.append('' if any(dead) else l); inside.append(bool(dead))
    return '\n'.join(keep), inside

def edges(root, files):
    p = get_parser('swift'); out = []
    for rel in files:
        src, inside = _prep((Path(root) / rel).read_text(encoding='utf-8', errors='replace'))
        st = [p.parse(src.encode()).root_node]
        while st:
            n = st.pop(); st.extend(n.children)
            L = n.start_point[0] + 1
            if n.type == 'class_declaration':
                kw = next((c.type for c in n.children if c.type in ('class', 'struct', 'enum', 'actor', 'extension')), None)
                nm = n.child_by_field_name('name')
                if kw in (None, 'extension') or nm is None or inside[L - 1]: continue
                out.append(dict(e='class', file=rel, line=L, name=nm.text.decode().split('<')[0], kind=kw))
            elif n.type == 'import_declaration':
                ids = [c for c in n.named_children if c.type == 'identifier']
                kw = next((c for c in n.children if c.type == 'import'), n); L = kw.start_point[0] + 1   # attributes before `import` not in range
                if inside[L - 1]: continue
                out.append(dict(e='import', file=rel, line=L, name=ids[0].text.decode() if ids else n.text.decode()))
    return out
