"""The tree-sitter module's findings as rules: what CRG's parser misses in a file, added to that file's output.

Same logic as core-module/merge.py (functions) + core-module/merge_edges.py (classes, inherits, imports, calls),
but per file, before anything is stored, so nothing has to be merged into a graph.db afterwards. The module code
itself is in rules/module_src/ (copied unchanged).

Rows already in CRG's output for the file are skipped, with the same keys as the survey scoring:
function = name with a CRG start line inside the module's span, class = name, inherits = class + short super,
import = line, call = line + short callee (multisets). Everything added carries extra.source = 'ts-module' and
extra.rule = <rule name>; unnamed functions also carry extra.unnamed = True (the reference counts named ones only).

Each kind is its own rule, so any of them can be switched off: module.functions, module.contains-unlinked,
module.classes, module.inherits, module.imports, module.calls.
"""
from __future__ import annotations

import bisect
import hashlib
import importlib
import logging
import re
import sys
from collections import Counter
from pathlib import Path

from . import rule

logger = logging.getLogger(__name__)
SRC = Path(__file__).with_name('module_src')
TAG = {'source': 'ts-module'}
DOTTED, SPACED = {'r', 'xslt', 'robot'}, {'robot'}
TEST_CALLS = ('describe(', 'it(', 'test(', 'context(', 'suite(', 'specify(')


def _src():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))


def _modref(lang):
    _src()
    try:
        return importlib.import_module(f'modref_{lang}')
    except ModuleNotFoundError:
        return None


def short(t, lang=''):
    """Same short-name rule as survey_lab._last / merge_edges.short."""
    t = re.sub(r':S[0-9a-f]{6,}(:\d+)?$', '', str(t).strip())
    if '::' in t:
        t = t.rsplit('::', 1)[1]
    t = t.split('\\')[-1] if '\\' in t else t
    if t.startswith(('<', '(', '[')):
        return t
    t = re.split(r'[<(\[]' if lang in SPACED else r'[<(\[\s]', t, maxsplit=1)[0].strip()
    if lang not in DOTTED:
        t = t.rsplit('.', 1)[-1] if '.' in t.strip('.') else t
    return t


_OBJC = re.compile(rb'^\s*(@interface|@protocol|@implementation|#import)\b', re.M)
_CPP = re.compile(rb'^\s*(class|namespace|template|struct\s+\w+\s*(final\s*)?:)\b|\w::\w|^\s*(public|private|protected):', re.M)


def _mlang(ctx):
    """The module language for this file. CRG labels most .h headers 'c' (it needs strong C++ evidence), but a
    header in a C++ or Objective-C codebase is parsed with that language's module rules, as the merge did
    (it used the repo's language for every file); decided here from the header's own text."""
    def make():
        lang = ctx.language
        if lang == 'c' and str(ctx.path).lower().endswith('.h'):
            if _OBJC.search(ctx.source):
                return 'objc'
            if _CPP.search(ctx.source):
                return 'cpp'
        return lang
    return ctx.cache('module_language', make)


def _where(ctx):
    """(root, rel) for the module code, which reads root / rel from disk (the same bytes CRG just parsed)."""
    p = Path(ctx.path).resolve()
    root = Path(ctx.repo_root).resolve() if ctx.repo_root else p.parent
    try:
        return root, str(p.relative_to(root))
    except ValueError:
        return p.parent, p.name


def _module_rows(ctx):
    """modref_<lang>.edges() rows for this file (cached for the module rules of one file)."""
    def make():
        m = _modref(_mlang(ctx))
        if m is None:
            return []
        root, rel = _where(ctx)
        return m.edges(root, [rel])
    return ctx.cache('module_rows', make)


def _qn(ctx, kind, name, parent=None):
    from ..graph import qualified_name_of
    from ..parser import NodeInfo
    return qualified_name_of(NodeInfo(kind=kind, name=name, file_path=str(ctx.path), line_start=0, line_end=0,
                                      parent_name=parent))


def _free(ctx, name, line, parent=None, kind='Class'):
    """name if its qualified name is free in this file's output, else CRG's own :S<hash> suffix."""
    taken = {_qn_of(n) for n in ctx.nodes}
    if _qn(ctx, kind, name, parent) not in taken:
        return name
    return f"{name}:S{hashlib.sha1(f'{ctx.path}:{line}:{name}'.encode()).hexdigest()[:8]}"


def _qn_of(n):
    from ..graph import qualified_name_of
    return qualified_name_of(n)


def _fp(ctx):
    from ..parser import NodeInfo
    return NodeInfo(kind='File', name=str(ctx.path), file_path=str(ctx.path), line_start=0, line_end=0).file_path


def _module_functions(ctx):
    """core-module extract.module_functions() rows for this file, or None if the module's parser rejects it."""
    def make():
        _src()
        import extract
        root, rel = _where(ctx)
        try:
            return extract.module_functions(root, rel, _mlang(ctx))
        except Exception:
            return None
    return ctx.cache('module_functions', make)


# ---------------------------------------------------------------- functions

SAME_LINE = 'js.functions-same-line'


def _same_line_on(ctx):
    """JavaScript only, switch js.functions-same-line (CRG_RULES_OFF=js.functions-same-line turns it off)."""
    from . import switch_on
    return ctx.language == 'javascript' and switch_on(SAME_LINE)


def _crg_functions_by_line(ctx):
    """CRG's own Function nodes (not Test, not added by a rule) by start line, for _merge_same_line."""
    out = {}
    for n in ctx.nodes:
        if n.kind == 'Function' and 'source' not in (n.extra or {}):
            out.setdefault(int(n.line_start or 0), []).append(n)
    return out


def _merge_same_line(ctx, r, named, same_line):
    """One node per function: when CRG already has a Function node starting on the module row's line (CRG named it
    differently), do not add a second node.
    - named row (CRG: exports.html, module: html): keep CRG's node as it is.
    - unnamed row (CRG named an arrow callback after its parameter, cid => ...): CRG's node becomes the unnamed one,
      renamed to the module's name (map#1) and marked unnamed; edges that pointed at the old name follow it.
    Returns True when the row was merged."""
    cands = [c for c in same_line.get(int(r['line_start']), []) if id(c) not in same_line.get('claimed', set())]
    if not cands:
        return False
    # the CRG node that is this function: same end line first; a node a named module row already matched by name
    # (const sleep = (ms) => ...: CRG's sleep) is never taken for an inner unnamed arrow on the same line
    n = next((c for c in cands if int(c.line_end or 0) == int(r['line_end'])), cands[0])
    same_line[int(r['line_start'])].remove(n)
    if named and not _param_named(ctx, n):
        return True                                 # exports.html vs html: CRG's name is a fair name; keep it
    old_qn, old_name = _qn_of(n), n.name
    taken = {_qn_of(m) for m in ctx.nodes}
    name = r['name']
    for cand in (name, f"{name}({r.get('arity', 0)})", f"{name}({r.get('arity', 0)})@{int(r['line_start'])}"):
        if _qn(ctx, 'Function', cand, n.parent_name) not in taken:
            name = cand
            break
    n.name = name
    # identity_name wins over name in the qualified name (graph.qualified_name_of); without this the node kept
    # CRG's qualified name (file::listener) and CRG's bare-name pass still bound listener(data) - a call to the
    # parameter - to it
    n.identity_name = None
    ctx.tag(n, source='ts-module', name=r['name'], unnamed=not named, crg_name=old_name, merged=SAME_LINE)
    new_qn = _qn_of(n)
    # Only this node's own edges follow it: two CRG nodes can share a qualified name inside one file (the store
    # adds the :S<hash> suffix later), so an edge is moved only when it is this node's CONTAINS (on its line)
    # or a call made from inside its lines.
    lo, hi = int(n.line_start or 0), int(n.line_end or n.line_start or 0)
    for e in ctx.edges:
        if e.kind == 'CONTAINS' and e.target == old_qn and int(e.line or 0) == lo:
            e.target = new_qn
        elif e.source == old_qn and lo <= int(e.line or 0) <= hi:
            e.source = new_qn
    same_line.setdefault('renamed', {})[old_qn] = old_name
    return True


_PARAM_ARROW = '(^|[(,\\s:=])\\(?\\s*{n}\\s*\\)?\\s*=>|\\(\\s*{n}\\s*,[^)]*\\)\\s*=>'


def _param_named(ctx, n):
    """True when CRG named an arrow function after its (first) parameter: `fix: fixer => ...` stored as `fixer`."""
    if '.' in n.name:
        return False
    lines = ctx.cache('source_lines', lambda: ctx.source.decode('utf-8', errors='replace').splitlines())
    ln = int(n.line_start or 0)
    text = lines[ln - 1] if 0 < ln <= len(lines) else ''
    return re.search(_PARAM_ARROW.format(n=re.escape(n.name)), text) is not None


def _unlink_renamed(ctx, same_line):
    """Calls CRG bound, while parsing, to a node it named after a parameter (listener(data) -> listener) point at
    a name no function has once that node is renamed. Unless another node still has that qualified name, such a
    call goes back to its bare name and is marked unresolved, so CRG's bare-name pass leaves it alone."""
    renamed = same_line.get('renamed') or {}
    if not renamed:
        return
    still = {_qn_of(m) for m in ctx.nodes}
    for e in ctx.edges:
        if e.kind == 'CALLS' and e.target in renamed and e.target not in still:
            e.target = renamed[e.target]
            ctx.tag(e, unresolved_targets=[], unlinked_from=SAME_LINE)

@rule('*', 'module.functions', kind='add')
def functions(ctx):
    """Function/Test nodes the module finds and CRG's parser does not (named ones it missed, every unnamed one),
    with CONTAINS from their class or file. As core-module/merge.py (write_calls=False)."""
    from ..parser import EdgeInfo, NodeInfo
    rows = _module_functions(ctx)
    if rows is None:                                # a file the module's parser rejects: CRG's output stays
        return
    have = {}
    for n in ctx.nodes:
        if n.kind in ('Function', 'Test'):
            have.setdefault(n.name, []).append(n.line_start)
    fpath = _fp(ctx)
    same_line = _crg_functions_by_line(ctx) if _same_line_on(ctx) else {}
    if same_line:                                   # CRG nodes a named module row matches by name: never merged
        named_rows = [r for r in rows if r.get('named', True)]
        same_line['claimed'] = {id(n) for ns in list(same_line.values()) for n in ns
                                if any(r['name'] == n.name and r['line_start'] <= n.line_start <= r['line_end']
                                       for r in named_rows)}
    for r in rows:
        named = r.get('named', True)
        if named and any(r['line_start'] <= ls <= r['line_end'] for ls in have.get(r['name'], [])):
            continue
        if same_line and _merge_same_line(ctx, r, named, same_line):
            continue
        label = r.get('label') or r['name']
        is_test = (not named) and label.startswith(TEST_CALLS)
        scope = r.get('scope') or None
        taken = {_qn_of(n) for n in ctx.nodes}
        name = r['name']
        for cand in (name, f"{name}({r.get('arity', 0)})", f"{name}({r.get('arity', 0)})@{int(r['line_start'])}"):
            if _qn(ctx, 'Function', cand, scope) not in taken:
                name = cand
                break
        node = NodeInfo(kind='Test' if is_test else 'Function', name=name, file_path=fpath,
                        line_start=int(r['line_start']), line_end=int(r['line_end']), language=ctx.crg_language,
                        parent_name=scope, params=f"({r.get('arity', 0)})", is_test=is_test,
                        extra={**TAG, 'name': r['name'], 'unnamed': not named})
        ctx.tag(node)
        ctx.nodes.append(node)
        parent = _qn(ctx, 'Class', scope) if scope and _qn(ctx, 'Class', scope) in taken else fpath
        ctx.edges.append(ctx.tag(EdgeInfo(kind='CONTAINS', source=parent, target=_qn_of(node), file_path=fpath,
                                          line=int(r['line_start']), extra=dict(TAG))))
        have.setdefault(r['name'], []).append(int(r['line_start']))
    if same_line:
        _unlink_renamed(ctx, same_line)


@rule('*', 'module.contains-unlinked', kind='fix')
def contains_unlinked(ctx):
    """CONTAINS from the file (or parent) for a Function/Test node CRG made but never linked (e.g. ReScript), when
    the module finds that function too (same name and start line). As merge_edges.py step 0."""
    from ..parser import EdgeInfo
    found = {(r['name'], int(r['line_start'])) for r in (_module_functions(ctx) or []) if r.get('named', True)}
    linked = {e.target for e in ctx.edges if e.kind == 'CONTAINS'}
    qns = {_qn_of(n) for n in ctx.nodes}
    fpath = _fp(ctx)
    for n in ctx.nodes:
        if n.kind not in ('Function', 'Test'):
            continue
        qn = _qn_of(n)
        if qn in linked or (n.name, int(n.line_start or 0)) not in found:
            continue
        parent = _qn(ctx, 'Class', n.parent_name) if n.parent_name else fpath
        parent = parent if parent in qns else fpath
        ctx.edges.append(ctx.tag(EdgeInfo(kind='CONTAINS', source=parent, target=qn, file_path=fpath,
                                          line=int(n.line_start or 0), extra=dict(TAG))))
        linked.add(qn)


# ---------------------------------------------------------------- classes, inherits, imports, calls

def _have_classes(ctx):
    return Counter(n.name for n in ctx.nodes if n.kind == 'Class')


def _class_node(ctx, name, line, line_end, kind):
    from ..parser import EdgeInfo, NodeInfo
    fpath = _fp(ctx)
    node = NodeInfo(kind='Class', name=_free(ctx, name, line), file_path=fpath, line_start=int(line),
                    line_end=int(line_end or line), language=ctx.crg_language, extra={**TAG, 'name': name, 'kind': kind})
    ctx.tag(node)
    ctx.nodes.append(node)
    ctx.edges.append(ctx.tag(EdgeInfo(kind='CONTAINS', source=fpath, target=_qn_of(node), file_path=fpath,
                                      line=int(line), extra=dict(TAG))))
    return node


def _take(counter, key):
    if counter[key] > 0:
        counter[key] -= 1
        return False
    return True


@rule('*', 'module.classes', kind='add')
def classes(ctx):
    """Class nodes the module finds and CRG's parser does not, with CONTAINS file -> class."""
    have = _have_classes(ctx)
    for r in (r for r in _module_rows(ctx) if r['e'] == 'class'):
        if _take(have, r['name']):
            _class_node(ctx, r['name'], r['line'], r.get('line_end'), r.get('kind', ''))


ANON_CLASS = 'js.inherits-anonymous-class'


def _anon_class_on(ctx):
    """JavaScript only, switch js.inherits-anonymous-class."""
    from . import switch_on
    return ctx.language == 'javascript' and switch_on(ANON_CLASS)


@rule('*', 'module.inherits', kind='add')
def inherits(ctx):
    """INHERITS the module finds and CRG's parser does not. Source = the nearest class node with that name (one is
    created where the inheritance is declared if there is none, e.g. a Perl package); target = the super's name."""
    from ..parser import EdgeInfo
    lang = ctx.language
    names = {_qn_of(n): n for n in ctx.nodes}
    have = Counter((getattr(names.get(e.source), 'name', None), short(e.target, lang))
                   for e in ctx.edges if e.kind == 'INHERITS')
    fpath = _fp(ctx)
    for r in (r for r in _module_rows(ctx) if r['e'] == 'inherits'):
        if not _take(have, (r['cls'], short(r['sup'], lang))):
            continue
        cands = [n for n in ctx.nodes if n.kind == 'Class' and (n.extra or {}).get('name', n.name) in (r['cls'],)
                 or n.kind == 'Class' and n.name == r['cls']]
        if not r['cls'] and _anon_class_on(ctx):
            # an anonymous class (window.X = class extends Y {}): use the class node on that line; never a nameless one
            cands = [n for n in ctx.nodes if n.kind == 'Class' and int(n.line_start or 0) == int(r['line'])]
            if not cands:                           # no class node there: the file is the source, as for top-level code
                ctx.edges.append(ctx.tag(EdgeInfo(kind='INHERITS', source=fpath, target=str(r['sup']), file_path=fpath,
                                                  line=int(r['line']), extra=dict(TAG))))
                continue
        if not cands:
            cands = [_class_node(ctx, r['cls'], r['line'], r['line'], 'from inherits')]
        src = min(cands, key=lambda n: abs((n.line_start or 0) - int(r['line'])))
        ctx.edges.append(ctx.tag(EdgeInfo(kind='INHERITS', source=_qn_of(src), target=str(r['sup']), file_path=fpath,
                                          line=int(r['line']), extra=dict(TAG))))


@rule('*', 'module.imports', kind='add')
def imports(ctx):
    """IMPORTS_FROM file -> name for import lines CRG's parser has no import edge on."""
    from ..parser import EdgeInfo
    have = Counter(int(e.line or 0) for e in ctx.edges if e.kind == 'IMPORTS_FROM')
    fpath = _fp(ctx)
    for r in (r for r in _module_rows(ctx) if r['e'] == 'import'):
        if _take(have, int(r['line'])):
            ctx.edges.append(ctx.tag(EdgeInfo(kind='IMPORTS_FROM', source=fpath, target=str(r.get('name') or ''),
                                              file_path=fpath, line=int(r['line']), extra=dict(TAG))))


@rule('*', 'calls.line-from-module', kind='fix')
def call_line_from_module(ctx):
    """CRG files every call of a multi-line statement under the statement's first line (`request(app)` /
    `.get(..)` / `.expect(..)`, or 12 `New QualityItem(..)` on one VB.NET line). When the module has the same call
    (same short name) on a later line of that statement and CRG has no such call there, move CRG's edge to the
    module's line. Moves only; never adds or drops an edge."""
    lang = ctx.language
    mod = Counter((int(r['line']), short(r['name'], lang)) for r in _module_rows(ctx) if r['e'] == 'call')
    if not mod:
        return
    crg = Counter((int(e.line or 0), short(e.target, lang)) for e in ctx.edges if e.kind == 'CALLS')
    free = mod - crg                                       # module calls CRG has no edge for on that exact line
    if not free:
        return
    surplus = crg - mod                                    # CRG edges the module does not have on that line
    lines = ctx.cache('lines', lambda: ctx.source.decode('utf-8', 'replace').splitlines())
    from .all_languages import _statement
    span = {}
    for e in ctx.edges:
        if e.kind != 'CALLS' or not e.line:
            continue
        k = (int(e.line), short(e.target, lang))
        if surplus[k] <= 0:
            continue
        if e.line not in span:
            span[e.line] = e.line + _statement(lines, e.line).count('\n')
        later = sorted(l for (l, n), c in free.items() if c > 0 and n == k[1] and e.line < l <= span[e.line])
        if not later:
            continue
        surplus[k] -= 1
        free[(later[0], k[1])] -= 1
        e.line = later[0]
        e.extra = {**(e.extra or {}), 'rule': ctx.rule, 'line_from': k[0]}


@rule('*', 'module.calls', kind='add')
def calls(ctx):
    """CALLS the module finds and CRG's parser does not. Source = the innermost function/class around the line
    (else the file); target = the callee's short name, resolved later by CRG like its own call targets.
    A call on a line that already has the same (source, target) gets the next negative source_offset so it is
    stored as its own row (see calls.keep-same-line-repeats)."""
    from ..parser import EdgeInfo
    lang = ctx.language
    have = Counter((int(e.line or 0), short(e.target, lang)) for e in ctx.edges if e.kind == 'CALLS')
    spans = sorted((n.line_start, n.line_end or n.line_start, _qn_of(n)) for n in ctx.nodes
                   if n.kind in ('Function', 'Test', 'Class') and n.line_start is not None)
    used = Counter((e.source, e.target, int(e.line or 0)) for e in ctx.edges if e.kind == 'CALLS')
    fpath = _fp(ctx)
    for r in (r for r in _module_rows(ctx) if r['e'] == 'call'):
        line, name = int(r['line']), short(r['name'], lang)
        if not _take(have, (line, name)):
            continue
        i = bisect.bisect_right(spans, (line, float('inf'), ''))
        inside = [s for s in spans[:i] if s[1] >= line]
        src = min(inside, key=lambda s: s[1] - s[0])[2] if inside else fpath
        k = (src, name, line)
        extra = dict(TAG)
        if used[k]:
            extra['source_offset'] = -1000 - used[k]       # never clashes with the repeats rule's -1, -2 ...
        used[k] += 1
        ctx.edges.append(ctx.tag(EdgeInfo(kind='CALLS', source=src, target=name, file_path=fpath, line=line,
                                          extra=extra)))
