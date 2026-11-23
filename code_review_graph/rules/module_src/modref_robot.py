"""Module edges for Robot Framework: no tree-sitter grammar; reuses the module's line-rule approach for Robot
(core-module/extract.py robot_functions: sections + cells split on 2+ spaces / tab). Conventions as edgeref_robot.py:
import = Library / Resource / Variables setting (name = first argument cell); call = each body step whose keyword cell
(after `${var}=` assignments) is not a control word or [setting]; line = start = step line."""
import re
from pathlib import Path
LABEL = "line rule (module's Robot approach, no grammar): Settings Library/Resource/Variables imports, body-step keyword calls"
SECTION = re.compile(r'^\*+\s*(.+?)\s*\**\s*$')
SPLIT = re.compile(r'\s{2,}|\t|\s\|\s')
CONTROL = {'FOR', 'END', 'IF', 'ELSE', 'ELSE IF', 'WHILE', 'TRY', 'EXCEPT', 'FINALLY', 'BREAK', 'CONTINUE', 'RETURN',
           'VAR', 'GROUP', 'IN', 'IN RANGE', 'IN ENUMERATE', 'IN ZIP', 'AND'}
ASSIGN = re.compile(r'^[$@&]\{[^}]*\}\s*=?\s*$')

def _cells(ln):
    return [c for c in SPLIT.split(ln.strip()) if c != '']

def edges(root, files):
    out = []
    for rel in files:
        try: lines = (Path(root) / rel).read_text(encoding='utf-8', errors='replace').splitlines()
        except Exception: continue
        if lines and lines[0].startswith('\ufeff'): lines[0] = lines[0][1:]
        sec, suite_tmpl, tmpl = None, False, False
        for ln in lines:   # Settings `Test Template` / `Task Template` templates every test without its own [Template]
            c = _cells(ln)
            if len(c) > 1 and c[0].lower() in ('test template', 'task template') and c[1].upper() != 'NONE': suite_tmpl = True
        for i, ln in enumerate(lines, 1):
            m = SECTION.match(ln)
            if m and ln.startswith('*'):
                sec = m.group(1).lower().rstrip('s'); continue
            s = ln.strip()
            if not s or s.startswith('#') or s.startswith('...'): continue
            if sec in ('setting',):
                c = _cells(ln)
                if c and c[0].lower() in ('library', 'resource', 'variables') and len(c) > 1:
                    out.append(dict(e='import', file=rel, line=i, name=c[1]))
            elif sec in ('keyword', 'test case', 'task'):
                c = _cells(ln)
                if not ln[0].isspace():                    # a test/keyword name: templated rows are not KeywordCalls
                    tmpl = suite_tmpl and sec != 'keyword'
                    if len(c) > 1 and c[1].lower() == '[template]': tmpl = len(c) > 2 and c[2].upper() != 'NONE'
                    c = c[1:]
                    if not c: continue
                if c and c[0].lower() == '[template]':
                    tmpl = len(c) > 1 and c[1].upper() != 'NONE'; continue
                if tmpl and c and not c[0].startswith('[') and c[0] not in CONTROL: continue
                if c and c[0] == 'IF' and len(c) > 2:      # inline IF: IF  cond  Kw  ...  ELSE  Kw
                    rest = c[2:]
                    while rest:
                        while rest and ASSIGN.match(rest[0]): rest = rest[1:]
                        if rest and rest[0] not in CONTROL: out.append(dict(e='call', file=rel, line=i, start=i, name=rest[0]))
                        k = next((j for j, x in enumerate(rest) if x in ('ELSE', 'ELSE IF')), None)
                        if k is None: break
                        rest = rest[k + (2 if rest[k] == 'ELSE IF' else 1):]
                    continue
                if c and c[0] == '\\': c = c[1:]
                while c and ASSIGN.match(c[0]): c = c[1:]
                if not c or c[0].startswith('[') or c[0] in CONTROL or c[0].startswith('#'): continue
                out.append(dict(e='call', file=rel, line=i, start=i, name=c[0]))
    return out
