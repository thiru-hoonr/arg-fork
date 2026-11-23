"""Post-build pass that binds Python calls whose target is fixed by import syntax (our fork).

Stock CRG (with its own python_resolver) leaves these cross-file Python calls unbound (notebooks/12-python):

- package re-exports: `from flask import Flask` where `flask/__init__.py` does `from .app import Flask`
- module attributes: `from . import cli; cli.AppGroup()`, `import flask; flask.Flask(...)`
- `@typing.overload` stubs: CRG stores the stubs and the real def as same-name duplicates (`f`, `f:S<hash>`) and
  leaves the call AMBIGUOUS; Python's last definition (the real one) is meant

What this pass does, on the stored graph, for .py files only:

1. Parse each file with tree-sitter (no text search): its top-level def/class (last one wins), its imports
   (`import a.b [as m]`, `from [..]a import f [as g]`) and its call sites.
2. A module name resolves the way Python's import path does: `<root>/a/b.py` or `<root>/a/b/__init__.py`, every
   folder in between a package, roots = repo root, `src/`, and non-package folders of .py files. Used only when
   exactly one file matches; relative imports resolve from the importing file's package.
3. A call is bound only when its target is certain from syntax: `f()` / `C()` for an imported name, `m.f()` for a
   module alias (also `a.b.f()`), or `C.m()` when the imported class C defines m itself. A name the calling file
   also binds another way (assignment, parameter, loop target) or defines itself is skipped.
4. The target is the CRG node (Function/Class/Test) starting on the definition line. The CRG CALLS edge on the call's
   line (or the attribute name's line, for a call over several lines) with that call name is rewritten to it, only
   when its target is not already a real node (or is a same-name duplicate of it, `f` vs `f:S<hash>`). Its
   claimed_endpoint / ambiguous / collision keys are dropped. Nothing is invented: a call with no CRG edge stays so.

Iteration 2, rule ``py.fix-wrong`` (switch via rules.switch_on; needs py.import-resolver on): when the import makes
the target certain and CRG bound the edge to a DIFFERENT real node (e.g. `login(request, ...)` inside
`Client.login` bound to `Client.login` itself, though `login` is the imported `django.contrib.auth.login`), the edge
is rewritten to the certain target, tagged ``rule='py.fix-wrong'``, ``collision_evidence='py.fix-wrong'`` and
``replaced`` (the old target).

Every rewritten edge gets ``extra.rule = 'py.import-resolver'`` and ``extra.py_import`` (the target module file).
Switch: rule name ``py.import-resolver``; off with ``CRG_RULES=off`` or ``CRG_RULES_OFF=py.import-resolver``; with
``CRG_RULES_ONLY`` set it runs only when named there. Called by incremental.py's FORK_LANG_RESOLVERS hook.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

RULE = 'py.import-resolver'
FIX_RULE = 'py.fix-wrong'     # Iteration 2 (see module docstring)


def enabled() -> bool:
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return False
    names = lambda v: {s.strip() for s in os.environ.get(v, '').split(',') if s.strip()}  # noqa: E731
    only, off = names('CRG_RULES_ONLY'), names('CRG_RULES_OFF')
    if only:
        return RULE in only
    return RULE not in off


def get_parser(lang):
    from tree_sitter_language_pack import get_parser as gp
    return gp(lang)


# ---- the same import model as lib/py_callers_ref.py (kept in step by hand; the fork cannot import lib/) ----
def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _dotted(n):
    """`a.b.c` attribute chain of identifiers -> 'a.b.c'; anything else -> None."""
    if n is None:
        return None
    if n.type == 'identifier':
        return _t(n)
    if n.type == 'attribute':
        o = _dotted(n.child_by_field_name('object'))
        a = n.child_by_field_name('attribute')
        return f'{o}.{_t(a)}' if o and a is not None else None
    return None


def parse_file(path):
    src = Path(path).read_bytes()
    tree = get_parser('python').parse(src)
    defs = {}        # top-level 'f' / 'C' and 'C.m' -> def line
    imports = {}     # local name -> (dots, module path, member or None)
    calls = []
    rebound = set()  # names also bound by assignment / parameter / for / with / except anywhere in the file

    def add_import_from(n):
        mod_n = n.child_by_field_name('module_name')
        mod = _t(mod_n)
        dots = len(mod) - len(mod.lstrip('.'))
        mod = mod.lstrip('.')
        for c in n.children_by_field_name('name'):
            if c.type == 'dotted_name':
                imports[_t(c)] = (dots, mod, _t(c))
            elif c.type == 'aliased_import':
                nm, al = c.child_by_field_name('name'), c.child_by_field_name('alias')
                imports[_t(al)] = (dots, mod, _t(nm))

    def add_import(n):
        for c in n.children_by_field_name('name'):
            if c.type == 'dotted_name':
                full = _t(c)
                head = full.split('.', 1)[0]
                imports.setdefault(head, (0, head, None))      # `import a.b` binds `a`
            elif c.type == 'aliased_import':
                nm, al = c.child_by_field_name('name'), c.child_by_field_name('alias')
                imports[_t(al)] = (0, _t(nm), None)

    def top_def(n, prefix=None):
        node = n.child_by_field_name('definition') if n.type == 'decorated_definition' else n
        if node is None or node.type not in ('function_definition', 'class_definition'):
            return
        name = _t(node.child_by_field_name('name'))
        key = f'{prefix}.{name}' if prefix else name
        defs[key] = node.start_point[0] + 1     # last definition wins, as in Python (`@overload` stubs come first)
        if node.type == 'class_definition' and prefix is None:
            body = node.child_by_field_name('body')
            for m in (body.named_children if body is not None else []):
                top_def(m, name)

    def bind_targets(n):
        if n is None:
            return
        if n.type == 'identifier':
            rebound.add(_t(n))
        elif n.type in ('pattern_list', 'tuple_pattern', 'list_pattern', 'expression_list', 'tuple', 'list',
                        'list_splat_pattern', 'as_pattern_target'):
            for c in n.named_children:
                bind_targets(c)

    def walk(n, depth):
        t = n.type
        if t in ('assignment', 'augmented_assignment'):
            bind_targets(n.child_by_field_name('left'))
        elif t in ('for_statement', 'for_in_clause'):
            bind_targets(n.child_by_field_name('left'))
        elif t == 'as_pattern':
            bind_targets(n.child_by_field_name('alias'))
        elif t == 'named_expression':
            bind_targets(n.child_by_field_name('name'))
        elif t in ('parameters', 'lambda_parameters'):
            for c in n.named_children:
                bind_targets(c if c.type == 'identifier' else (c.child_by_field_name('name') or
                             (c.named_children[0] if c.named_children else None)))
        if t == 'import_from_statement':
            add_import_from(n)
        elif t == 'import_statement':
            add_import(n)
        elif t == 'call':
            fn = n.child_by_field_name('function')
            line = n.start_point[0] + 1
            if fn is not None and fn.type == 'identifier':
                calls.append({'line': line, 'pline': line, 'kind': 'bare', 'name': _t(fn)})
            elif fn is not None and fn.type == 'attribute':
                recv = _dotted(fn.child_by_field_name('object'))
                a = fn.child_by_field_name('attribute')
                if recv and a is not None:
                    calls.append({'line': line, 'pline': a.start_point[0] + 1, 'kind': 'member',
                                  'recv': recv, 'name': _t(a)})
        for c in n.children:
            walk(c, depth + 1)

    root = tree.root_node
    for c in root.named_children:                  # top-level definitions only, the module's own names
        top_def(c)
        if c.type == 'if_statement' or c.type == 'try_statement':   # `try: from x import f` blocks still bind f
            pass
    walk(root, 0)
    # an import inside a function (`from .testing import FlaskClient as cls`) that the file also binds another
    # way (`cls = self.test_client_class`) depends on control flow, so it is not certain
    return {'defs': defs, 'imports': imports, 'calls': calls, 'rebound': rebound}


def source_roots(files, root):
    """Directories on the import path: the repo root, `src/` when present, and every directory that holds .py
    files but is not itself a package (a script or test folder imports its siblings by bare name)."""
    dirs = {os.path.dirname(f) for f in files}
    roots = {root.rstrip('/')}
    if os.path.isdir(root + 'src'):
        roots.add(root + 'src')
    for d in dirs:
        if not os.path.exists(os.path.join(d, '__init__.py')):
            roots.add(d)
    return roots


def module_index(files, root):
    """Absolute module name -> file: `a.b` is <source root>/a/b.py or <source root>/a/b/__init__.py, used only when
    exactly one file matches across all source roots. (Matching any path suffix would send stdlib `import json`
    inside `flask/json/provider.py` to `flask/json/__init__.py`.)"""
    roots = source_roots(files, root)
    by_mod = defaultdict(set)
    for f in files:
        for r in roots:
            if not f.startswith(r + '/'):
                continue
            rel = f[len(r) + 1:][:-3]
            parts = rel.split('/')
            if parts[-1] == '__init__':
                parts = parts[:-1]
            if not parts:
                continue
            # every directory between the root and the file must be a package (has __init__.py)
            d, ok = r, True
            for seg in parts[:-1] if not f.endswith('__init__.py') else parts:
                d = os.path.join(d, seg)
                if not os.path.exists(os.path.join(d, '__init__.py')):
                    ok = False
                    break
            if ok:
                by_mod['.'.join(parts)].add(f)
    return {m: next(iter(s)) for m, s in by_mod.items() if len(s) == 1}


def resolve_module(dots, mod, from_file, mod2file, fileset):
    """File for module `mod` (absolute when dots == 0, else relative to from_file's package)."""
    if dots:
        base = os.path.dirname(from_file)
        for _ in range(dots - 1):
            base = os.path.dirname(base)
        path = os.path.join(base, *mod.split('.')) if mod else base
        for cand in (path + '.py', os.path.join(path, '__init__.py')):
            if cand in fileset:
                return cand
        return None
    return mod2file.get(mod)


class Index:
    def __init__(self, files, root):
        self.files = [f for f in files]
        self.fileset = set(files)
        self.root = root if root.endswith('/') else root + '/'
        self.P = {}
        for f in files:
            try:
                self.P[f] = parse_file(f)
            except (OSError, ValueError):
                pass
        self.mod2file = module_index(files, self.root)

    def binding(self, f, name, seen=0):
        """What local `name` in file f refers to: ('module', file) | ('def', file, key) | None."""
        p = self.P.get(f)
        if p is None or seen > 5:
            return None
        if name in p['defs']:
            return ('def', f, name)
        imp = p['imports'].get(name)
        if imp is None:
            return None
        dots, mod, member = imp
        if member is None:                           # import a.b as m / import a
            mf = resolve_module(dots, mod, f, self.mod2file, self.fileset)
            return ('module', mf) if mf else None
        # from mod import member: a submodule, or a name in the module
        sub = resolve_module(dots, f'{mod}.{member}' if mod else member, f, self.mod2file, self.fileset)
        if sub:
            return ('module', sub)
        mf = resolve_module(dots, mod, f, self.mod2file, self.fileset)
        if not mf or mf == f:
            return None
        return self.binding(mf, member, seen + 1)

    def module_attr(self, mf, name):
        """`name` looked up in module file mf: def, re-exported def, or submodule."""
        b = self.binding(mf, name)
        if b:
            return b
        pkg = os.path.dirname(mf) if mf.endswith('__init__.py') else None
        if pkg:
            for cand in (os.path.join(pkg, name + '.py'), os.path.join(pkg, name, '__init__.py')):
                if cand in self.fileset:
                    return ('module', cand)
        return None

    def resolve_call(self, f, c):
        """(target file, def key) when certain from syntax, else None."""
        p = self.P[f]
        if c['kind'] == 'bare':
            if c['name'] in p['defs'] or c['name'] not in p['imports'] or c['name'] in p['rebound']:
                return None
            b = self.binding(f, c['name'])
            return (b[1], b[2]) if b and b[0] == 'def' else None
        parts = c['recv'].split('.')
        if parts[0] in p['defs'] or parts[0] not in p['imports'] or parts[0] in p['rebound']:
            return None
        b = self.binding(f, parts[0])
        for seg in parts[1:]:
            if b is None or b[0] != 'module':
                return None
            b = self.module_attr(b[1], seg)
        if b is None:
            return None
        if b[0] == 'module':
            got = self.module_attr(b[1], c['name'])
            return (got[1], got[2]) if got and got[0] == 'def' else None
        # receiver is an imported class: C.m() only when C defines m itself
        tf, key = b[1], b[2]
        mk = f'{key}.{c["name"]}'
        return (tf, mk) if mk in self.P[tf]['defs'] else None



def _base(qn):
    return re.sub(r':S[0-9a-f]+$', '', qn)


def _short(target):
    tail = target.rsplit('::', 1)[-1]
    tail = re.sub(r':S[0-9a-f]+$', '', tail)
    return tail.rsplit('.', 1)[-1]


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'files': 0, 'certain_calls': 0, 'calls_resolved': 0,
             'already_bound': 0, 'no_crg_edge': 0, 'no_target_node': 0, 'bound_elsewhere': 0,
             'wrong_fixed': 0}
    if not stats['enabled']:
        return stats
    from .rules import switch_on
    fix_wrong = switch_on(FIX_RULE)
    stats['fix_wrong_enabled'] = fix_wrong
    all_files = store.get_all_files()
    files = [f for f in all_files if f.endswith('.py')]
    if not files:
        return stats
    root = os.path.commonpath(all_files) if len(all_files) > 1 else os.path.dirname(files[0])
    if os.path.isfile(root):
        root = os.path.dirname(root)
    ix = Index(files, root + '/')
    stats['files'] = len(ix.P)
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    fileset = set(files)
    node_at = defaultdict(list)
    for row in conn.execute("SELECT name, qualified_name, file_path, line_start FROM nodes "
                            "WHERE kind IN ('Function','Class','Test')"):
        if row['file_path'] in fileset:
            node_at[(row['file_path'], row['line_start'])].append((row['name'], row['qualified_name']))
    node_names = {r[0] for r in conn.execute('SELECT qualified_name FROM nodes')}
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        if row['file_path'] in fileset:
            edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))

    updates, done = [], set()
    for f, p in ix.P.items():
        for c in p['calls']:
            r = ix.resolve_call(f, c)
            if not r or r[0] == f:
                continue
            tf, key = r
            stats['certain_calls'] += 1
            nodes = node_at.get((tf, ix.P[tf]['defs'][key]), [])
            if len(nodes) > 1:
                want = key.rsplit('.', 1)[-1]
                nodes = [nd for nd in nodes if nd[0] == want] or nodes
            if len(nodes) != 1:
                stats['no_target_node'] += 1
                continue
            target = nodes[0][1]
            cands = [e for ln in dict.fromkeys((c['line'], c['pline'])) for e in edges_at.get((f, ln), [])
                     if _short(e[1]) == c['name'] and e[0] not in done]
            if not cands:
                stats['no_crg_edge'] += 1
                continue
            if any(e[1] == target and 'collision_endpoint' not in (e[2] or '') for e in cands):
                stats['already_bound'] += 1
                continue
            for eid, tq, extra in cands:
                wrong = tq in node_names and _base(tq) != _base(target)
                if wrong and not fix_wrong:
                    stats['bound_elsewhere'] += 1
                    continue    # Iteration 1: CRG bound it to another real node; not ours to override
                d = json.loads(extra or '{}') or {}
                for k in ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count',
                          'ambiguous_targets_truncated', 'unresolved_targets', 'collision_endpoint'):
                    d.pop(k, None)
                if wrong:       # Iteration 2: the import proves CRG's binding wrong; replace it
                    d.update({'rule': FIX_RULE, 'collision_evidence': FIX_RULE, 'py_import': tf, 'replaced': tq})
                    stats['wrong_fixed'] += 1
                else:
                    d.update({'rule': RULE, 'py_import': tf})
                updates.append((target, json.dumps(d), eid))
                done.add(eid)
                break

    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('Python import resolution: %s', stats)
    return stats
