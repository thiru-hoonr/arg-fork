"""Module edges for ReScript: no tree-sitter grammar in tree_sitter_language_pack; reuses the module's line-rule
approach (core-module/extract.py rescript_functions). Conventions as edgeref_rescript.py (bsc parsetree):
import = `open M` that is a structure item (not `open` inside a function body); call = lowercase identifier (or M.f)
directly followed by `(`, or a bare `->f` pipe target; name = last part; constructors, keywords, decorators skipped."""
import re
from pathlib import Path
LABEL = "line rule (module's ReScript approach, no grammar): structure-level open, ident( / ->ident applications"
KW = {'if', 'switch', 'while', 'for', 'fun', 'function', 'let', 'and', 'in', 'when', 'else', 'try', 'catch', 'assert', 'lazy',
      'await', 'async', 'external', 'type', 'module', 'open', 'include', 'rec', 'mutable', 'list', 'dict'}
CALL = re.compile(r"(?<![\w.@#'`~%])((?:[A-Z][\w']*\.)*)([a-z_][\w']*)(?:\s*)\(")
PIPE = re.compile(r"->\s*((?:[A-Z][\w']*\.)*)([a-z_][\w']*)(?![\w'(.])(?!\s*\()")
OPEN = re.compile(r"^\s*open!?\s+([A-Z][\w.]*)")

def _blank(src):
    # blank comments and string/template literals (keep newlines) so regexes only see code
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith('//', i):
            j = src.find('\n', i); j = n if j < 0 else j; out.append(' ' * (j - i)); i = j
        elif src.startswith('/*', i):
            depth, j = 1, i + 2              # ReScript block comments nest
            while j < n and depth:
                if src.startswith('/*', j): depth += 1; j += 2
                elif src.startswith('*/', j): depth -= 1; j += 2
                else: j += 1
            out.append(re.sub(r'[^\n]', ' ', src[i:j])); i = j
        elif c in '"`':
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == '\\' else 1
            j = min(j + 1, n); out.append(c + re.sub(r'[^\n]', ' ', src[i + 1:j - 1]) + c if j - i >= 2 else src[i:j]); i = j
        else:
            out.append(c); i += 1
    return ''.join(out)

def edges(root, files):
    out = []
    for rel in files:
        try: src = _blank((Path(root) / rel).read_text(encoding='utf-8', errors='replace'))
        except Exception: continue
        # brace stack: True = a module body `module X = {` (structure), False = expression block
        stack, pending_mod = [], False
        for i, ln in enumerate(src.split('\n'), 1):
            m = OPEN.match(ln)
            if m and all(stack):
                out.append(dict(e='import', file=rel, line=i, name=m.group(1)))
            for mm in CALL.finditer(ln):
                if mm.group(2) in KW: continue
                out.append(dict(e='call', file=rel, line=i, start=i, name=mm.group(2)))
            for mm in PIPE.finditer(ln):
                if mm.group(2) in KW: continue
                out.append(dict(e='call', file=rel, line=i, start=i, name=mm.group(2)))
            for tok in re.finditer(r"\bmodule\s+(?:type\s+)?[A-Z][\w']*\s*(?::[^={]*)?=\s*\{|[{}]", ln):
                t = tok.group()
                if t == '}':
                    if stack: stack.pop()
                else:
                    stack.append(t != '{')
    return out
