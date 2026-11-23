"""Module classes and edges for VB.NET. There is no tree-sitter VB grammar (tree_sitter_language_pack has none), so like
core-module/extract.py (vbnet_functions) these are LINE RULES. Same row contract as edgeref_vbnet.py; rules follow Roslyn VB."""
import re
from pathlib import Path
LABEL = "line rules (no tree-sitter VB grammar): Class/Structure/Interface/Module/Enum lines (+ attribute lines above), Inherits/Implements, Imports, name( invocations"
MODS = r'(?:(?:Public|Private|Friend|Protected|Shared|Shadows|MustInherit|NotInheritable|Partial|Default|ReadOnly|Overloads)\s+)*'
CLS = re.compile(r'^\s*(?:<[^>]*>\s*)*' + MODS + r'(Class|Structure|Interface|Module|Enum)\s+\[?(\w+)', re.I)
INH = re.compile(r'^\s*(Inherits|Implements)\s+(.*)$', re.I)
IMP = re.compile(r'^\s*Imports\s+\S', re.I)
KW = {'if', 'ctype', 'directcast', 'trycast', 'gettype', 'nameof', 'sub', 'function', 'new', 'as', 'of', 'cint', 'cstr', 'cbool',
      'cdbl', 'clng', 'cobj', 'cdate', 'cdec', 'cbyte', 'cchar', 'csng', 'cshort', 'cuint', 'culng', 'cushort', 'csbyte', 'getxmlnamespace',
      'and', 'or', 'not', 'andalso', 'orelse', 'in', 'is', 'isnot', 'return', 'then', 'xor', 'mod', 'property', 'event', 'declare',
      'operator', 'call', 'raiseevent', 'handles', 'implements', 'to', 'step', 'each', 'while', 'until', 'case', 'typeof', 'await', 'like', 'set', 'get', 'addhandler', 'removehandler', 'using', 'synclock', 'elseif', 'until', 'string', 'integer', 'byte', 'object', 'boolean', 'double', 'single', 'long', 'short', 'char', 'date', 'decimal', 'sbyte', 'ushort', 'uinteger', 'ulong'}
CALL = re.compile(r'(?<![\w#])(\.)?\s*\[?([A-Za-z_]\w*)\]?\s*(?:\(\s*Of\b[^()]*\)\s*\(|\((?!\s*Of\b))', re.I)
DECL = re.compile(r'^\s*(?:<[^>]*>\s*)*(?:(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|NotOverridable|MustOverride|Overloads|Shadows|Static|Async|Iterator|Partial|Default|ReadOnly|WriteOnly|Narrowing|Widening|Dim|Const|WithEvents)\s+)*(Sub|Function|Operator|Property|Event|Declare|Delegate)\b', re.I)

def _str(m):
    if m.group(0).startswith('$'):        # interpolated string: keep the {expressions}
        return ' '.join(re.findall(r'\{([^{}]*)\}', m.group(0)))
    return '""'

def _code(ln):
    ln = re.sub(r'\$?"(?:[^"]|"")*"', _str, ln)
    return re.sub(r"(?:'|\bREM\b).*$", '', ln)

def _simple(t):
    t = re.split(r'[(<]', t.strip(), 1)[0].strip(); return t.rsplit('.', 1)[-1]

def edges(root, files):
    out = []
    for rel in files:
        lines = (Path(root) / rel).read_text(encoding='utf-8-sig', errors='replace').splitlines()
        cur = None
        for i, raw in enumerate(lines, 1):
            ln = _code(raw)
            m = CLS.match(ln)
            if m:
                s = i
                while s > 1 and re.match(r'^\s*<.*>\s*_?\s*$', _code(lines[s - 2])): s -= 1
                cur = m.group(2) if m.group(1).lower() != 'enum' else None
                out.append(dict(e='class', file=rel, line=s, name=m.group(2), kind=m.group(1).lower()))
                cur = (cur, s)
                continue
            h = INH.match(ln)
            if h and cur and cur[0]:
                for part in re.split(r',(?![^()]*\))', h.group(2)):
                    if part.strip(): out.append(dict(e='inherits', file=rel, line=cur[1], cls=cur[0], sup=_simple(part)))
                continue
            if ln.strip() and not h: cur = (None, 0) if cur and not re.match(r'^\s*$', ln) else cur
            if IMP.match(ln):
                out.append(dict(e='import', file=rel, line=i, name=ln.strip()[8:])); continue
            ln = re.sub(r'^\s*(?:<[^<>]*(?:<[^<>]*>[^<>]*)*>\s*_?\s*)+', ' ', ln)   # attribute lists are not invocations
            ln = re.sub(r'<\s*\w+(?:\s*\([^()]*\))?(?:\s*,\s*\w+(?:\s*\([^()]*\))?)*\s*>', ' ', ln)   # inline attributes
            if re.match(r'^\s*<\w', ln): ln = ''   # continued attribute list
            d = DECL.match(ln)
            body = ln[d.end():] if d else ln
            if d:
                pm = re.match(r'\s*\[?\w+\]?\s*(?:\(Of[^()]*\))?\s*\(', body)   # skip declared name's parameter list
                if pm: body = body[pm.end():]
            for c in CALL.finditer(body):
                dot = body[:c.start(2)].rstrip(' [').endswith('.')
                if c.group(2).lower() in KW and not dot: continue
                pre = body[:c.start()].rstrip()
                if re.search(r'\b(As|New|Of|Inherits|Implements|Sub|Function)\s+[\w.]*$|\b(As|New|Of)\s*$', pre, re.I): continue
                out.append(dict(e='call', file=rel, line=i, start=i, name=c.group(2)))
    return out
