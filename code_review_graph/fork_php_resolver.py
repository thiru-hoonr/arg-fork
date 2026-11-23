"""Post-build pass that binds PHP calls whose target PHP's own name rules fix (our fork; notebooks/20-php).

What stock CRG does with PHP (notebooks/20-php: guzzle, symfony-console, laravel, WordPress):

- Namespaced code with ``use`` imports is mostly resolved by CRG's scoped_resolver (``Class::m()``, same-namespace
  classes), but ``new Foo(...)`` and ``parent::__construct(...)`` are often left as bare UNRESOLVED names.
- Code without namespaces that loads files with ``require_once`` (WordPress) is almost entirely unresolved: a call
  to a global function such as ``wp_insert_post()`` defined in another file stays a bare name, because CRG binds
  only "same file or one imported file" and ``require_once`` is not an import edge to it.

What this pass does, on the stored graph, for .php files only (same rules as lib/php_callers_ref.py, copied here
because the fork cannot import lib/):

1. Parse every indexed .php file with tree-sitter: namespaces, ``use`` imports (classes, ``use function``, group
   uses), classes / interfaces / traits / enums with their methods, parent and traits, functions, and call sites.
2. A call is certain when PHP's name rules fix it:
   - ``Foo::bar()``, ``self::bar()``, ``parent::bar()``: the class is resolved as PHP does and declared once in the
     repo; ``bar`` is the first declaration found walking class -> traits -> parent inside the repo. Never
     ``static::`` (late static binding), never when the walk leaves the repo first or finds nothing (``__callStatic``).
   - ``new Foo(...)``: the class, declared once -> its Class node.
   - ``foo()``: ``use function`` first, then ``<namespace>\foo``, then the global ``foo`` (PHP's fallback), defined
     exactly once. Global functions are unique at runtime, so a function defined once in the repo is the target
     even without an import (WordPress). Functions defined under a test directory are targets only for test callers.
3. The CRG CALLS edge on the call's line (or the name's line, for a call over several lines) with the call's name
   (exact case) is rewritten to the target node when its target is not a real node (bare or AMBIGUOUS) or is a
   same-name duplicate (``X`` vs ``X:S<hash>``). Nothing is invented: a call with no CRG edge stays without one.

Iteration 2, rule ``php.fix-wrong`` (on by default; off with ``CRG_RULES_OFF=php.fix-wrong``): when the target is
certain, the call is the only call with that name on its line, and CRG bound the edge to a DIFFERENT real node, the
edge is rewritten too, tagged ``rule = 'php.fix-wrong'`` and ``php_replaced``.

Every rewritten edge drops ``claimed_endpoint``, ambiguous lists and ``collision_endpoint`` and gets
``collision_evidence``, so CRG's collision pass keeps it (``crg.keep-resolver-evidence`` in graph.py).
Edges get ``extra.rule = 'php.import-resolver'`` and ``extra.php_via`` (function / Class::m() / self / parent / new).
Switch: ``CRG_RULES=off``, ``CRG_RULES_OFF=php.import-resolver``, or ``CRG_RULES_ONLY``.
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

RULE = 'php.import-resolver'
FIX_RULE = 'php.fix-wrong'          # Iteration 2
DROP = ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count', 'ambiguous_targets_truncated',
        'unresolved_targets', 'collision_endpoint')


def _on(name: str) -> bool:
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return False
    names = lambda v: {s.strip() for s in os.environ.get(v, '').split(',') if s.strip()}  # noqa: E731
    only, off = names('CRG_RULES_ONLY'), names('CRG_RULES_OFF')
    return name in only if only else name not in off


def enabled() -> bool:
    return _on(RULE)


# ---- parsing and certainty: identical to lib/php_callers_ref.py -------------------------------------------------
CLASSES = ('class_declaration', 'interface_declaration', 'trait_declaration', 'enum_declaration')


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _name_of(n):
    """The written name of a name / qualified_name / relative_name node."""
    return _t(n).strip()


def resolve_class(raw, ns, uses):
    """PHP class-name resolution -> lowercased FQN without leading backslash (None for self/static/parent)."""
    raw = raw.strip()
    low = raw.lower()
    if low in ('self', 'static', 'parent'):
        return None
    if raw.startswith('\\'):
        return low[1:]
    if low.startswith('namespace\\'):
        return ((ns + '\\') if ns else '') + low[len('namespace\\'):]
    first, _, rest = low.partition('\\')
    if first in uses:
        return uses[first] + (('\\' + rest) if rest else '')
    return ((ns + '\\') if ns else '') + low


def parse_source(src: bytes, parser):
    tree = parser.parse(src)
    classes, funcs, calls = [], [], []

    def uses_from(decl, cls_uses, fn_uses):
        kw_fn = any(c.type == 'function' for c in decl.children)
        prefix = ''
        for c in decl.children:
            if c.type == 'namespace_name':
                prefix = _t(c).lower().strip('\\') + '\\'
        clauses = []
        for c in decl.children:
            if c.type == 'namespace_use_clause':
                clauses.append(c)
            elif c.type == 'namespace_use_group':
                clauses += [g for g in c.children if g.type == 'namespace_use_clause']
        for cl in clauses:
            is_fn = kw_fn or any(x.type == 'function' for x in cl.children)
            is_const = any(x.type == 'const' for x in cl.children) or any(x.type == 'const' for x in decl.children)
            named = [x for x in cl.children if x.type in ('qualified_name', 'name', 'namespace_name')]
            if not named:
                continue
            full = (prefix + _t(named[0]).lower().strip('\\'))
            alias = cl.child_by_field_name('alias')
            if alias is None and len(named) > 1 and any(x.type == 'as' for x in cl.children):
                alias = named[-1]
            short = (_t(alias) if alias is not None else full.rsplit('\\', 1)[-1]).lower()
            if is_const:
                continue
            (fn_uses if is_fn else cls_uses)[short] = full

    def walk_block(nodes, ns, cls_uses, fn_uses):
        for n in nodes:
            if n.type == 'namespace_definition':
                nm = n.child_by_field_name('name')
                new_ns = _t(nm).lower().strip('\\') if nm is not None else ''
                body = n.child_by_field_name('body')
                if body is not None:
                    walk_block(body.children, new_ns, {}, {})
                else:
                    ns, cls_uses, fn_uses = new_ns, {}, {}
                continue
            if n.type == 'namespace_use_declaration':
                uses_from(n, cls_uses, fn_uses)
                continue
            walk(n, ns, cls_uses, fn_uses, None)

    def walk(n, ns, cu, fu, cls):
        t = n.type
        line = n.start_point[0] + 1
        if t in CLASSES:
            nm = n.child_by_field_name('name')
            if nm is not None:
                fqn = ((ns + '\\') if ns else '') + _t(nm).lower()
                ext, traits, methods = None, [], {}
                for c in n.children:
                    if c.type == 'base_clause':
                        names = [x for x in c.children if x.type in ('name', 'qualified_name')]
                        if names:
                            ext = resolve_class(_name_of(names[0]), ns, cu)
                body = n.child_by_field_name('body')
                for m in (body.children if body is not None else []):
                    if m.type == 'use_declaration':
                        traits += [resolve_class(_name_of(x), ns, cu) for x in m.children
                                   if x.type in ('name', 'qualified_name')]
                    elif m.type == 'method_declaration':
                        mn = m.child_by_field_name('name')
                        if mn is not None:
                            methods.setdefault(_t(mn).lower(), []).append((_t(mn), m.start_point[0] + 1))
                classes.append({'fqn': fqn, 'name': _t(nm), 'line': line, 'extends': ext,
                                'traits': [x for x in traits if x], 'methods': methods})
                cls = fqn
        elif t == 'function_definition':
            nm = n.child_by_field_name('name')
            if nm is not None:
                funcs.append((((ns + '\\') if ns else '') + _t(nm).lower(), _t(nm), line))
        elif t == 'scoped_call_expression':
            scope, nm = n.child_by_field_name('scope'), n.child_by_field_name('name')
            if scope is not None and nm is not None and nm.type == 'name':
                st = _t(scope).strip().lower()
                if scope.type == 'relative_scope' or st in ('self', 'static', 'parent'):
                    rel = st
                    target_cls = None
                elif scope.type in ('name', 'qualified_name', 'relative_name'):
                    rel, target_cls = None, resolve_class(_name_of(scope), ns, cu)
                else:
                    rel, target_cls = 'dynamic', None
                calls.append({'line': line, 'pline': nm.start_point[0] + 1, 'kind': 'static', 'rel': rel,
                              'cls': target_cls, 'enclosing': cls, 'name': _t(nm)})
        elif t == 'object_creation_expression':
            named = [c for c in n.children if c.type in ('name', 'qualified_name', 'relative_name')]
            if named:
                raw = _name_of(named[0])
                rel = raw.lower() if raw.lower() in ('self', 'static', 'parent') else None
                calls.append({'line': line, 'pline': named[0].start_point[0] + 1, 'kind': 'new', 'rel': rel,
                              'cls': resolve_class(raw, ns, cu), 'enclosing': cls,
                              'name': raw.rsplit('\\', 1)[-1]})
        elif t in ('member_call_expression', 'nullsafe_member_call_expression'):
            nm = n.child_by_field_name('name')
            if nm is not None and nm.type == 'name':
                calls.append({'line': line, 'pline': nm.start_point[0] + 1, 'kind': 'member', 'name': _t(nm),
                              'enclosing': cls})
        elif t == 'function_call_expression':
            fn = n.child_by_field_name('function')
            if fn is not None and fn.type in ('name', 'qualified_name', 'relative_name'):
                raw = _name_of(fn)
                low = raw.lower()
                if raw.startswith('\\'):
                    cands = [low[1:]]
                elif low.startswith('namespace\\'):
                    cands = [((ns + '\\') if ns else '') + low[len('namespace\\'):]]
                elif '\\' in low:
                    first, _, rest = low.partition('\\')
                    cands = [(cu[first] + '\\' + rest) if first in cu else (((ns + '\\') if ns else '') + low)]
                elif low in fu:
                    cands = [fu[low]]
                else:
                    cands = ([ns + '\\' + low] if ns else []) + [low]
                calls.append({'line': line, 'pline': line, 'kind': 'func', 'cands': cands, 'enclosing': cls,
                              'name': raw.rsplit('\\', 1)[-1]})
        for c in n.children:
            walk(c, ns, cu, fu, cls)

    walk_block(tree.root_node.children, '', {}, {})
    return {'classes': classes, 'funcs': funcs, 'calls': calls}


def parse_file(path, parser=None):
    return parse_source(Path(path).read_bytes(), parser)


def build_index(P):
    """class fqn -> [(file, class dict)], function fqn -> [(file, name, line)]"""
    cls_idx, fn_idx = defaultdict(list), defaultdict(list)
    for f, p in P.items():
        for c in p['classes']:
            cls_idx[c['fqn']].append((f, c))
        for fqn, name, line in p['funcs']:
            fn_idx[fqn].append((f, name, line))
    return cls_idx, fn_idx


def find_method(cls_idx, fqn, mname, seen=None):
    """Walk class -> traits -> parent inside the repo. Returns (file, class name, method name, line) or None
    (not found, ambiguous, or the walk left the repo before finding it)."""
    seen = seen or set()
    if fqn is None or fqn in seen:
        return None
    seen.add(fqn)
    decl = cls_idx.get(fqn, [])
    if len(decl) != 1:
        return None
    f, c = decl[0]
    ms = c['methods'].get(mname.lower(), [])
    if len(ms) == 1:
        return (f, c['name'], ms[0][0], ms[0][1])
    if len(ms) > 1:
        return None
    for tr in c['traits']:
        hit = find_method(cls_idx, tr, mname, seen)
        if hit:
            return hit
    if c['extends']:
        return find_method(cls_idx, c['extends'], mname, seen)
    return None


TEST_DIRS = ('/tests/', '/test/', '/Tests/', '/phpunit/')


def is_test(path):
    return any(d in path for d in TEST_DIRS)


def certain_calls(P):
    """The certain cross-file calls of a parsed repo (P: file -> parse_file result)."""
    cls_idx, fn_idx = build_index(P)
    # Test suites define stubs of real functions inside the code's namespace (guzzle tests/bootstrap.php defines
    # GuzzleHttp\Handler\curl_setopt to fake curl). Those only exist when the test bootstrap is loaded, so a
    # function defined under a test directory is a target only for callers that are themselves test files.
    # The same holds for classes: a test double declared under tests/ with a production class's name.
    fn_prod, cls_prod = defaultdict(list), defaultdict(list)
    for k, defs in fn_idx.items():
        fn_prod[k] = [d for d in defs if not is_test(d[0])]
    for k, decl in cls_idx.items():
        cls_prod[k] = [d for d in decl if not is_test(d[0])]
    cls_all = cls_idx
    out, objects = [], 0
    for f, p in P.items():
        idx = fn_idx if is_test(f) else fn_prod
        cls_idx = cls_all if is_test(f) else cls_prod
        per_line = defaultdict(int)                    # calls of one name on one line (any kind)
        for c in p['calls']:
            for ln in {c['line'], c['pline']}:
                per_line[(ln, c['name'])] += 1
        for c in p['calls']:
            tgt = None
            if c['kind'] == 'member':
                continue
            if c['kind'] == 'func':
                for cand in c['cands']:
                    defs = idx.get(cand, [])
                    if len(defs) == 1:
                        tf, nm, ln = defs[0]
                        tgt = {'target_file': tf, 'target_line': ln, 'target_name': nm, 'target_class': None,
                               'via': 'function'}
                        break
                    if len(defs) > 1:
                        break
            elif c['kind'] == 'static':
                if c['rel'] in ('static', 'dynamic'):
                    continue
                if c['rel'] == 'self':
                    start = c['enclosing']
                elif c['rel'] == 'parent':
                    enc = cls_idx.get(c['enclosing'], [])
                    start = enc[0][1]['extends'] if len(enc) == 1 else None
                else:
                    start = c['cls']
                hit = find_method(cls_idx, start, c['name'])
                if hit:
                    tgt = {'target_file': hit[0], 'target_line': hit[3], 'target_name': hit[2],
                           'target_class': hit[1], 'via': c['rel'] or 'Class::m()'}
            elif c['kind'] == 'new':
                if c['rel'] in ('static',):
                    continue
                fqn = c['enclosing'] if c['rel'] == 'self' else c['cls']
                if c['rel'] == 'parent':
                    enc = cls_idx.get(c['enclosing'], [])
                    fqn = enc[0][1]['extends'] if len(enc) == 1 else None
                decl = cls_idx.get(fqn, []) if fqn else []
                if len(decl) == 1:
                    cf, cd = decl[0]
                    ctor = find_method(cls_idx, fqn, '__construct')
                    tgt = {'target_file': cf, 'target_line': cd['line'], 'target_name': cd['name'],
                           'target_class': None, 'via': 'new', 'ctor': ctor}
            if tgt and tgt['target_file'] != f:
                out.append({**c, **tgt, 'file': f,
                            'alone_on_line': per_line[(c['line'], c['name'])] == 1 and per_line[(c['pline'], c['name'])] == 1})
    return out



# ---- binding ------------------------------------------------------------------------------------------------------
def short(t):
    tail = t.rsplit('::', 1)[-1].split(':S', 1)[0]
    return tail.rsplit('.', 1)[-1].rsplit('\\', 1)[-1]


def _base(qn):
    return re.sub(r':S[0-9a-f]+$', '', qn)


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'fix_wrong_enabled': _on(FIX_RULE), 'files': 0, 'certain_calls': 0,
             'calls_resolved': 0, 'wrong_bindings_corrected': 0, 'already_bound': 0, 'no_crg_edge': 0,
             'no_target_node': 0}
    if not stats['enabled']:
        return stats
    files = [f for f in store.get_all_files() if f.lower().endswith('.php')]
    if not files:
        return stats
    from tree_sitter_language_pack import get_parser
    parser = get_parser('php')
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    P = {}
    for f in files:
        try:
            P[f] = parse_file(f, parser)
        except (OSError, ValueError) as exc:
            logger.warning('php resolver: cannot parse %s: %s', f, exc)
    stats['files'] = len(P)
    certain = certain_calls(P)
    stats['certain_calls'] = len(certain)

    node_at, node_named = defaultdict(list), defaultdict(list)
    for row in conn.execute("SELECT qualified_name, file_path, line_start, name, parent_name, kind FROM nodes "
                            "WHERE kind IN ('Function','Test','Class')"):
        node_at[(row['file_path'], row['line_start'])].append((row['qualified_name'], row['kind']))
        node_named[(row['file_path'], (row['name'] or '').lower(), (row['parent_name'] or '').lower())].append(
            (row['qualified_name'], row['kind']))
    node_names = {r[0] for r in conn.execute('SELECT qualified_name FROM nodes')}
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))

    fix_on, updates, used = _on(FIX_RULE), [], set()
    for c in certain:
        tn = c['target_name'].lower()
        if c['kind'] == 'new':                       # CRG binds `new X` to the class node
            cands = [q for q, k in node_at.get((c['target_file'], c['target_line']), []) if k == 'Class'
                     and short(q).lower() == tn] or [q for q, k in node_named.get((c['target_file'], tn, ''), [])
                                                     if k == 'Class']
        else:
            cands = [q for q, k in node_at.get((c['target_file'], c['target_line']), []) if short(q).lower() == tn]
            cands = cands or [q for q, k in node_named.get((c['target_file'], tn, (c['target_class'] or '').lower()), [])]
        cands = sorted(set(cands))
        if len(cands) != 1:
            stats['no_target_node'] += 1
            continue
        target = cands[0]
        here = [e for ln in dict.fromkeys((c['line'], c['pline'])) for e in edges_at.get((c['file'], ln), [])
                if short(e[1]) == c['name'] and e[0] not in used]
        if not here:
            stats['no_crg_edge'] += 1
            continue
        if any(e[1] == target and 'collision_endpoint' not in (e[2] or '') for e in here):
            stats['already_bound'] += 1
            continue
        for eid, tq, extra in here:
            if tq not in node_names or _base(tq) == _base(target):
                rule = RULE
            elif fix_on and c['alone_on_line']:
                rule = FIX_RULE
            else:
                continue
            d = json.loads(extra or '{}') or {}
            for k in DROP:
                d.pop(k, None)
            d.update({'rule': rule, 'php_via': c['via'], 'collision_evidence': rule})
            if rule == FIX_RULE:
                d['php_replaced'] = tq
                stats['wrong_bindings_corrected'] += 1
            updates.append((target, json.dumps(d), eid))
            used.add(eid)
            break

    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('PHP resolution: %s', stats)
    return stats
