"""Post-build pass that binds Kotlin calls whose target is fixed by package / import syntax (our fork).

Stock CRG stores Kotlin imports as raw names (``com.squareup.kotlinpoet.CodeBlock``), never as files, so its
"same file or exactly one imported file" rule cannot bind a cross-file Kotlin call: ``CodeBlock.of(...)``,
``ClassName(...)`` or a same-package top-level ``requireNoneOrOneOf(...)`` stay UNRESOLVED (notebooks/16-kotlin).

What this pass does, on the stored graph, for .kt files only:

1. Parse each file with tree-sitter: package, imports (explicit, aliased, star), top-level functions and classes,
   class / companion / object members, nested classes, parameter counts, expect/actual modifiers, call sites.
2. A call is bound only when its target is certain from syntax (Kotlin's name order: explicit import > same
   package > star import): a top-level function ``f()``, a constructor ``T()``, an object member ``O.m()``, a
   companion member ``C.m()``, a nested class ``C.N()``, or an extension ``x.f()`` imported by name; exactly one
   declaration must accept the argument count. Names the calling file declares, or that an enclosing class (or
   a repo supertype) declares as a member, are skipped. For ``expect`` + ``actual``s the target is ``expect``.
3. The target is the CRG node (Function/Class/Test) at the line of that declaration. The CRG CALLS edge on the
   call's line (or the called name's line) with that name is rewritten to it, only when its target is not
   already a real node (or is a same-name ``:S<hash>`` duplicate of it). ``claimed_endpoint``, ``ambiguous_*``,
   ``unresolved_targets`` and ``collision_endpoint`` are dropped from the edges it binds. Nothing is invented.

Parser note: tree-sitter-kotlin loses a class written ``class X`` + newline + ``private constructor(``; this pass
parses a copy with that newline moved (same length, same lines), so its own reading is right. CRG's parser is not
changed, so those classes have no CRG node and calls to them cannot be bound here.

The parsing code is a copy of lib/kotlin_callers_ref.py (the fork cannot import lib/); keep them in step.
Edges get ``extra.rule = 'kotlin.import-resolver'`` and ``extra.kotlin_import`` (how the name was found).
Switch: rule name ``kotlin.import-resolver``; off with ``CRG_RULES=off`` or ``CRG_RULES_OFF=kotlin.import-resolver``;
with ``CRG_RULES_ONLY`` set it runs only when named there.

Iteration 2, rule ``kotlin.fix-wrong`` (needs ``kotlin.import-resolver`` on): when the call's target is certain from
syntax and CRG had bound the edge to a DIFFERENT real node (e.g. ``KoinPlatform.getKoin()`` bound to a ``getKoin``
in the calling file), the edge is rewritten to the certain target and tagged ``rule='kotlin.fix-wrong'`` and
``collision_evidence='kotlin.fix-wrong'`` (so graph.py's collision pass, with ``crg.keep-resolver-evidence`` on,
keeps it). Without this rule such edges are counted ``bound_elsewhere`` and left alone.
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

RULE = 'kotlin.import-resolver'
FIX_RULE = 'kotlin.fix-wrong'


def fix_enabled() -> bool:
    try:
        from .rules import switch_on
    except ImportError:
        return False
    return switch_on(FIX_RULE)


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


_CTOR = re.compile(rb'\n([ \t]*)((?:(?:public|private|internal|protected)|@[A-Za-z_][\w.]*(?:\([^)\n]*\))?)'
                   rb'(?:[ \t]+(?:public|private|internal|protected|@[A-Za-z_][\w.]*(?:\([^)\n]*\))?))*)'
                   rb'([ \t]+)constructor\b')


def fix_ctor_newline(src: bytes) -> bytes:
    """`class X\\n  private constructor(` -> `class X  private\\nconstructor(`: same length, same line count."""
    def sub(m):
        ws, mods, gap = m.group(1), m.group(2), m.group(3)
        return b' ' + ws + mods + b'\n' + gap[1:] + b'constructor'
    return _CTOR.sub(sub, src)


def misparsed_classes(files):
    """Files where a class header is followed by `<modifier> constructor(` on the next line."""
    return sorted(f for f in files if _CTOR.search(Path(f).read_bytes()))


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _first(n, *types):
    for c in n.children:
        if c.type in types:
            return c
    return None


def _params(fn):
    """(required, total, vararg) for a function_declaration."""
    ps = _first(fn, 'function_value_parameters')
    if ps is None:
        return 0, 0, False
    req = total = 0
    vararg = False
    kids = ps.children
    for i, c in enumerate(kids):
        if c.type == 'parameter_modifiers' and 'vararg' in _t(c):
            vararg = True
        if c.type == 'parameter':
            total += 1
            nxt = kids[i + 1] if i + 1 < len(kids) else None
            if nxt is None or nxt.type != '=':
                req += 1
    if vararg:
        req = max(0, req - 1)
    return req, total, vararg


def _platform(n):
    m = _first(n, 'modifiers')
    if m is None:
        return None
    s = _t(m)
    return 'expect' if re.search(r'\bexpect\b', s) else 'actual' if re.search(r'\bactual\b', s) else None


def _nargs(call):
    suf = _first(call, 'call_suffix')
    if suf is None:
        return 0
    n = 0
    for c in suf.children:
        if c.type == 'value_arguments':
            n += sum(1 for a in c.children if a.type == 'value_argument')
        elif c.type == 'annotated_lambda':
            n += 1
    return n


def _supertypes(cls):
    names = []
    for c in cls.children:
        if c.type in ('delegation_specifier', 'delegation_specifiers'):
            for u in re.findall(r'\b([A-Z]\w*)', _t(c)):
                names.append(u)
    return names


def parse_file(path, parser=None):
    src = fix_ctor_newline(Path(path).read_bytes())
    parser = parser or get_parser('kotlin')
    root = parser.parse(src).root_node
    out = {'pkg': '', 'imports': {}, 'stars': [], 'funcs': [], 'classes': [], 'calls': [], 'declared': set(),
           'supers': {}}
    ph = _first(root, 'package_header')
    if ph is not None:
        ident = _first(ph, 'identifier')
        out['pkg'] = _t(ident).replace(' ', '') if ident is not None else ''
    il = _first(root, 'import_list')
    for h in (il.children if il is not None else []):
        if h.type != 'import_header':
            continue
        ident = _first(h, 'identifier')
        if ident is None:
            continue
        fq = _t(ident).replace(' ', '')
        if _first(h, 'wildcard_import') is not None:
            out['stars'].append(fq)
            continue
        al = _first(h, 'import_alias')
        alias = _t(_first(al, 'type_identifier', 'simple_identifier')) if al is not None else fq.rsplit('.', 1)[-1]
        out['imports'][alias] = fq
    for h in [c for c in root.children if c.type == 'import_header']:   # imports outside import_list
        ident = _first(h, 'identifier')
        if ident is not None and _first(h, 'wildcard_import') is None:
            fq = _t(ident).replace(' ', '')
            out['imports'].setdefault(fq.rsplit('.', 1)[-1], fq)

    def walk(n, owner, kind, chain):
        """owner: tuple of enclosing class names; kind: top|class|companion|object|local; chain: enclosing
        class paths (for implicit-receiver member checks)."""
        t = n.type
        if t == 'function_declaration':
            name = _first(n, 'simple_identifier')
            if name is not None:
                nm = _t(name)
                out['declared'].add(nm)
                req, total, va = _params(n)
                ext = _first(n, 'receiver_type') is not None
                out['funcs'].append({'name': nm, 'line': n.start_point[0] + 1, 'req': req, 'total': total,
                                     'vararg': va, 'ext': ext, 'platform': _platform(n), 'owner': owner,
                                     'kind': kind})
            for c in n.children:
                walk(c, owner, 'local', chain)
            return
        if t in ('class_declaration', 'object_declaration'):
            name = _first(n, 'type_identifier')
            nm = _t(name) if name is not None else None
            if nm:
                out['declared'].add(nm)
                ck = 'object' if t == 'object_declaration' else (
                    'interface' if _first(n, 'interface') is not None else
                    'enum' if _first(n, 'enum') is not None else 'class')
                path = owner + (nm,)
                out['classes'].append({'name': nm, 'line': n.start_point[0] + 1, 'kind': ck, 'path': path,
                                       'platform': _platform(n), 'top': kind == 'top', 'scope': kind})
                out['supers'][path] = _supertypes(n)
                for c in n.children:
                    if c.type in ('class_body', 'enum_class_body'):
                        walk(c, path, 'object' if ck == 'object' else 'class', chain + (path,))
                    else:
                        walk(c, owner, 'local', chain)
                return
        if t == 'companion_object':
            nm = _first(n, 'type_identifier')
            if nm is not None:
                out['declared'].add(_t(nm))
            for c in n.children:
                if c.type == 'class_body':
                    walk(c, owner, 'companion', chain)
            return
        if t in ('object_literal', 'lambda_literal', 'anonymous_function', 'function_body',
                 'anonymous_initializer', 'secondary_constructor', 'getter', 'setter'):
            for c in n.children:
                walk(c, owner, 'local', chain)
            return
        if t in ('variable_declaration', 'parameter', 'class_parameter'):
            nm = _first(n, 'simple_identifier')
            if nm is not None:
                out['declared'].add(_t(nm))
        if t == 'call_expression':
            callee = n.children[0] if n.children else None
            nargs = _nargs(n)
            line = n.start_point[0] + 1
            if callee is not None and callee.type == 'simple_identifier':
                out['calls'].append({'line': line, 'pline': line, 'kind': 'bare', 'recv': None,
                                     'name': _t(callee), 'nargs': nargs, 'chain': chain})
            elif callee is not None and callee.type == 'navigation_expression' and len(callee.children) == 2:
                recv, suf = callee.children
                prop = _first(suf, 'simple_identifier') if suf.type == 'navigation_suffix' else None
                if recv.type == 'simple_identifier' and prop is not None:
                    out['calls'].append({'line': line, 'pline': prop.start_point[0] + 1, 'kind': 'nav',
                                         'recv': _t(recv), 'name': _t(prop), 'nargs': nargs, 'chain': chain})
        for c in n.children:
            walk(c, owner, kind if t in ('source_file', 'class_body', 'enum_class_body', 'ERROR') else 'local',
                 chain)

    walk(root, (), 'top', ())
    return out


def _accepts(d, n):
    return d['req'] <= n and (d['vararg'] or n <= d['total'])


def _pick(cands, n):
    """One target (and its expect/actual group) from candidate declarations, or None if not unique."""
    ok = [d for d in cands if _accepts(d, n)] if any('req' in d for d in cands) else list(cands)
    if not ok:
        return None
    exp = [d for d in ok if d.get('platform') == 'expect']
    if len(exp) == 1 and all(d is exp[0] or d.get('platform') == 'actual' for d in ok):
        group = [d for d in cands if d is exp[0] or d.get('platform') == 'actual']
        return exp[0], group
    if len(ok) == 1:
        return ok[0], [ok[0]]
    return None


class Index:
    def __init__(self, files):
        self.parser = get_parser('kotlin')
        self.P = {}
        for f in files:
            try:
                self.P[f] = parse_file(f, self.parser)
            except (OSError, ValueError):
                pass
        self.top_funcs = defaultdict(list)       # (pkg, name) -> decls
        self.top_classes = defaultdict(list)     # (pkg, name) -> class decls
        self.members = defaultdict(lambda: defaultdict(list))   # (file, path, kind) -> name -> decls
        self.nested = defaultdict(dict)          # (file, path) -> name -> class decl
        self.member_names = defaultdict(set)     # simple class name -> member function names (repo)
        self.supers = {}
        for f, p in self.P.items():
            for d in p['funcs']:
                d = {**d, 'file': f, 'pkg': p['pkg']}
                if d['kind'] == 'top':
                    self.top_funcs[(p['pkg'], d['name'])].append(d)
                elif d['kind'] in ('class', 'companion', 'object') and d['owner']:
                    self.members[(f, d['owner'], d['kind'])][d['name']].append(d)
                    self.member_names[d['owner'][-1]].add(d['name'])
            for c in p['classes']:
                c = {**c, 'file': f, 'pkg': p['pkg']}
                if c['top']:
                    self.top_classes[(p['pkg'], c['name'])].append(c)
                elif len(c['path']) > 1 and c['scope'] in ('class', 'object', 'companion'):
                    self.nested[(f, c['path'][:-1])][c['name']] = c
            for path, sup in p['supers'].items():
                self.supers[(f, path)] = sup

    def _lookup(self, p, f, name, table):
        """Kotlin name order. Returns (candidates, how) or (None, why)."""
        if name in p['imports']:
            fq = p['imports'][name]
            pkg, real = fq.rsplit('.', 1) if '.' in fq else ('', fq)
            c = table.get((pkg, real), [])
            return (c, 'import') if c else (None, 'library import')
        same = [d for d in table.get((p['pkg'], name), [])]
        if same:
            return same, 'same package'
        hits = [(sp, table[(sp, name)]) for sp in p['stars'] if table.get((sp, name))]
        if len(hits) == 1:
            return hits[0][1], 'star import'
        return None, 'not in repo' if not hits else 'two star imports'

    def _implicit_member(self, call, f):
        """True when an enclosing class (or a repo supertype of it) declares a member with this name."""
        for path in call['chain']:
            if call['name'] in self.member_names.get(path[-1], ()):
                return True
            for s in self.supers.get((f, path), []):
                if call['name'] in self.member_names.get(s, ()):
                    return True
        return False

    def certain(self):
        out = []
        for f, p in self.P.items():
            for c in p['calls']:
                r = self.resolve_call(f, p, c)
                if r is None:
                    continue
                target, group, how = r
                if target['file'] == f:
                    continue
                out.append({'file': f, 'line': c['line'], 'pline': c['pline'], 'name': c['name'],
                            'kind': c['kind'], 'how': how, 'target_file': target['file'],
                            'target_line': target['line'], 'target_name': target['name'],
                            'group': [(g['file'], g['line']) for g in group],
                            'expect_actual': len(group) > 1})
        return out

    def resolve_call(self, f, p, c):
        name, n = c['name'], c['nargs']
        if c['kind'] == 'bare':
            if name in p['declared'] or self._implicit_member(c, f):
                return None
            cands, how = self._lookup(p, f, name, self.top_funcs)
            if cands:
                r = _pick(cands, n)
                return (r[0], r[1], how) if r else None
            if how != 'not in repo':
                return None
            cands, how = self._lookup(p, f, name, self.top_classes)      # constructor
            if cands:
                cls = [d for d in cands if d['kind'] in ('class', 'enum')] if all(
                    d['kind'] != 'interface' for d in cands) else []
                r = _pick(cls, n) if cls else None
                if r:
                    return r[0], r[1], how + ' (constructor)'
            return None
        # nav: X.m(...)
        recv = c['recv']
        if recv in p['declared']:
            return None
        cands, how = self._lookup(p, f, recv, self.top_classes)
        if cands:
            r = _pick(cands, 0)
            if r is None:
                return None
            cls = r[0]
            kind = 'object' if cls['kind'] == 'object' else 'companion'
            ms = self.members.get((cls['file'], cls['path'], kind), {}).get(name, [])
            if ms:
                r2 = _pick(ms, n)
                if not r2:
                    return None
                # members of an `expect` class/object: the same-named members of its `actual`s are the same target
                group = list(r2[1]) + [d for a in r[1] if a is not cls
                                       for d in self.members.get((a['file'], a['path'], kind), {}).get(name, [])]
                return r2[0], group, how + f' ({kind} member)'
            nc = self.nested.get((cls['file'], cls['path']), {}).get(name)
            if nc is not None and nc['kind'] in ('class', 'enum'):
                return nc, [nc], how + ' (nested class constructor)'
            return None
        # x.f(...) with f an extension function imported by name
        if recv[:1].islower() and name in p['imports']:
            fq = p['imports'][name]
            pkg, real = fq.rsplit('.', 1) if '.' in fq else ('', fq)
            ext = [d for d in self.top_funcs.get((pkg, real), []) if d['ext']]
            if ext and len(ext) == len(self.top_funcs[(pkg, real)]) and real not in self.member_names_all():
                r = _pick(ext, n)
                return (r[0], r[1], 'import (extension)') if r else None
        return None

    def member_names_all(self):
        if not hasattr(self, '_mna'):
            self._mna = set().union(*self.member_names.values()) if self.member_names else set()
        return self._mna



def _base(qn):
    """`file::C.m:S1a2b` -> `file::C.m` (CRG's suffix for a second definition with the same name)."""
    return re.sub(r'(:S[0-9a-f]+)(:\d+)?$', '', qn)


def _short(target):
    return target.rsplit('::', 1)[-1].split(':', 1)[0].rsplit('.', 1)[-1]


DROP = ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count', 'ambiguous_targets_truncated',
        'unresolved_targets', 'collision_endpoint')


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'files': 0, 'certain_calls': 0, 'calls_resolved': 0,
             'already_bound': 0, 'no_crg_edge': 0, 'no_target_node': 0, 'bound_elsewhere': 0,
             'wrong_fixed': 0, 'fix_enabled': False}
    if not stats['enabled']:
        return stats
    files = [f for f in store.get_all_files() if f.endswith('.kt')]
    if not files:
        return stats
    fix = stats['fix_enabled'] = fix_enabled()
    ix = Index(files)
    stats['files'] = len(ix.P)
    certain = ix.certain()
    stats['certain_calls'] = len(certain)
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
    for c in certain:
        nodes = node_at.get((c['target_file'], c['target_line']), [])
        if len(nodes) > 1:
            nodes = [nd for nd in nodes if nd[0] == c['target_name']] or nodes
        if len(nodes) != 1:
            stats['no_target_node'] += 1
            continue
        target = nodes[0][1]
        group = {q for g in c['group'] for _, q in node_at.get(g, [])}
        cands = [e for ln in dict.fromkeys((c['line'], c['pline'])) for e in edges_at.get((c['file'], ln), [])
                 if _short(e[1]) == c['name'] and e[0] not in done]
        if not cands:
            stats['no_crg_edge'] += 1
            continue
        if any(e[1] in group and 'collision_endpoint' not in (e[2] or '') for e in cands):
            stats['already_bound'] += 1
            continue
        wrong = []
        for eid, tq, extra in cands:
            if tq in node_names and _base(tq) != _base(target) and tq not in group:
                wrong.append((eid, tq, extra))
                continue   # CRG bound it to another real node; only Iteration 2 overrides that
            d = json.loads(extra or '{}') or {}
            for k in DROP:
                d.pop(k, None)
            updates.append((target, json.dumps({**d, 'rule': RULE, 'kotlin_import': c['how']}), eid))
            done.add(eid)
            break
        else:
            if wrong and fix:
                eid, tq, extra = wrong[0]
                d = json.loads(extra or '{}') or {}
                for k in DROP:
                    d.pop(k, None)
                updates.append((target, json.dumps({**d, 'rule': FIX_RULE, 'collision_evidence': FIX_RULE,
                                                    'kotlin_import': c['how'], 'crg_had': tq}), eid))
                done.add(eid)
                stats['wrong_fixed'] += 1
            elif wrong:
                stats['bound_elsewhere'] += 1
    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('Kotlin import resolution: %s', stats)
    return stats
