"""Post-build pass that binds JavaScript calls whose target is fixed by require/import + exports (our fork).

Stock CRG leaves these cross-file JS calls unbound (notebooks/10-javascript, eslint):

- ``const log = require("./shared/logging"); log.error()``: AMBIGUOUS, five functions named ``error``
- ``const assert = require("./shared/assert"); assert(x)``: the module exports one function (``ok``) under
  another name, so the bare name ``assert`` matches nothing
- ``const createCLIOptions = require("./options"); createCLIOptions()``: ``module.exports = function () {}``
- ``const express = require("../../"); express()``: the import points at a directory's index file

What this pass does, on the stored graph, for .js/.mjs/.cjs files only:

1. Parse each file with tree-sitter (no text search): its imports (``const X = require('./x')``,
   ``const {f, g: h} = require('./x')``, ``require('./x').f``, ``import X / * as X / {f as g} from './x'``),
   its exports (``module.exports = X | function | {...}``, ``exports.f``, ``module.exports.f``,
   ``export function / const / {} / default``) and its call sites.
2. A relative import resolves to exactly one repo file: ``./x``, ``./x.js``, ``.mjs``, ``.cjs``, ``./x/index.*``.
   Package imports are never touched.
3. A call is bound only when its target is certain from syntax: ``f()`` for an imported ``f``, ``X.f()`` for a
   module alias ``X``, ``require('./x').f()``, or ``X()`` when ``X`` is a module that exports one function.
   The target is the CRG node (Function/Class/Test) at the line where the target file defines that export,
   following re-exports of imports. When CRG stores two nodes on that line (``module.exports`` and ``exports``
   for ``module.exports = function``), the one named like the export, else ``module.exports``, is used. For a
   class export, ``X.m()`` means the static ``m`` when the class also has an instance ``m``.
4. The CRG CALLS edge on that line (or on the method name's line, for a call written over several lines) with
   that call name is rewritten to the node, only when its target is not already a real node (or is a same-name
   duplicate of the node, ``C.m`` vs ``C.m:S<hash>``, which CRG cannot tell apart). Its
   ``collision_endpoint`` is dropped, since the import says which definition is meant; otherwise CRG's later
   collision pass would turn it back into AMBIGUOUS. Nothing is invented: a call with no CRG edge stays so.

5. Iteration 2, rule ``js.fix-wrong``: when CRG already bound that edge to a *different* real node (CRG's own
   wrong answer), the edge is rewritten to the certain target too, tagged ``rule = 'js.fix-wrong'``,
   ``collision_evidence`` (so CRG's collision pass keeps it, see graph.py ``crg.keep-resolver-evidence``) and
   ``replaced_target`` (what CRG had). Skipped when the old target is the certain target of another call with the
   same name on the same line.

Every rewritten edge gets ``extra.rule = 'js.require-resolver'`` and ``extra.js_import`` (the import spec).
Switch: rule name ``js.require-resolver``; off with ``CRG_RULES=off`` or ``CRG_RULES_OFF=js.require-resolver``;
with ``CRG_RULES_ONLY`` set it runs only when named there.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

RULE = 'js.require-resolver'
FIX_RULE = 'js.fix-wrong'          # Iteration 2: correct CRG's own wrong binding of a certain call
JS_EXT = ('.js', '.mjs', '.cjs')
EXTS = ('', '.js', '.mjs', '.cjs', '/index.js', '/index.mjs', '/index.cjs')
FUNC = ('function_expression', 'arrow_function', 'function', 'generator_function', 'class')


def enabled() -> bool:
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return False
    names = lambda v: {s.strip() for s in os.environ.get(v, '').split(',') if s.strip()}  # noqa: E731
    only, off = names('CRG_RULES_ONLY'), names('CRG_RULES_OFF')
    if only:
        return RULE in only
    return RULE not in off


def _fix_wrong_enabled() -> bool:
    from .rules import switch_on
    return enabled() and switch_on(FIX_RULE)


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _str(n):
    if n is None or n.type != 'string':
        return None
    return _t(n)[1:-1]


def _require_of(n):
    """require('s') -> ('s', None); require('s').m -> ('s', 'm'); else None."""
    if n is None:
        return None
    if n.type == 'call_expression':
        fn, args = n.child_by_field_name('function'), n.child_by_field_name('arguments')
        if _t(fn) == 'require' and args is not None and args.named_child_count == 1:
            s = _str(args.named_children[0])
            return (s, None) if s else None
    if n.type == 'member_expression':
        inner = _require_of(n.child_by_field_name('object'))
        if inner and inner[1] is None:
            return (inner[0], _t(n.child_by_field_name('property')))
    return None


def _parse(src: bytes, parser):
    tree = parser.parse(src)
    defs, imports, exports, export_obj, calls = {}, {}, {}, [], []
    default_fn = None

    def walk(n):
        nonlocal default_fn
        t, line = n.type, n.start_point[0] + 1
        if t in ('function_declaration', 'generator_function_declaration', 'class_declaration'):
            name = n.child_by_field_name('name')
            if name is not None:
                defs[_t(name)] = line
                if t == 'class_declaration':
                    body = n.child_by_field_name('body')
                    for m in (body.named_children if body is not None else []):
                        if m.type == 'method_definition':
                            mk = f'{_t(name)}.{_t(m.child_by_field_name("name"))}'
                            # `Class.m()` on the class means the static m; an instance m of the same name loses
                            if any(c.type == 'static' for c in m.children) or mk not in defs:
                                defs[mk] = m.start_point[0] + 1
        elif t == 'variable_declarator':
            name, val = n.child_by_field_name('name'), n.child_by_field_name('value')
            if name is not None and val is not None:
                if name.type == 'identifier':
                    if val.type in FUNC:
                        defs[_t(name)] = line
                    r = _require_of(val)
                    if r:
                        imports[_t(name)] = r
                    if val.type == 'object':
                        for p in val.named_children:
                            v = p.child_by_field_name('value') if p.type == 'pair' else None
                            if v is not None and v.type in FUNC:
                                defs[f"{_t(name)}.{_t(p.child_by_field_name('key'))}"] = p.start_point[0] + 1
                            elif p.type == 'method_definition':
                                defs[f"{_t(name)}.{_t(p.child_by_field_name('name'))}"] = p.start_point[0] + 1
                elif name.type == 'object_pattern':
                    r = _require_of(val)
                    if r and r[1] is None:
                        for p in name.named_children:
                            if p.type == 'shorthand_property_identifier_pattern':
                                imports[_t(p)] = (r[0], _t(p))
                            elif p.type == 'pair_pattern':
                                k, v = p.child_by_field_name('key'), p.child_by_field_name('value')
                                if v is not None and v.type == 'identifier':
                                    imports[_t(v)] = (r[0], _t(k))
        elif t == 'assignment_expression':
            left, right = n.child_by_field_name('left'), n.child_by_field_name('right')
            chain = [left]
            while right is not None and right.type == 'assignment_expression':   # exports = module.exports = X
                chain.append(right.child_by_field_name('left'))
                right = right.child_by_field_name('right')
            for lf in chain:
                if lf is None or right is None:
                    continue
                if _t(lf) == 'module.exports':
                    if right.type == 'identifier':
                        export_obj.append(_t(right))
                    elif right.type in FUNC:
                        nm = right.child_by_field_name('name')
                        default_fn = _t(nm) if nm is not None else 'module.exports'
                        defs[default_fn] = line
                    elif right.type == 'object':
                        for p in right.named_children:
                            if p.type == 'shorthand_property_identifier':
                                exports[_t(p)] = _t(p)
                            elif p.type == 'pair':
                                v, k = p.child_by_field_name('value'), _t(p.child_by_field_name('key'))
                                if v is not None and v.type == 'identifier':
                                    exports[k] = _t(v)
                                elif v is not None and v.type in FUNC:
                                    defs[f'module.exports.{k}'] = p.start_point[0] + 1
                                    exports[k] = f'module.exports.{k}'
                            elif p.type == 'method_definition':
                                k = _t(p.child_by_field_name('name'))
                                defs[f'module.exports.{k}'] = p.start_point[0] + 1
                                exports[k] = f'module.exports.{k}'
                elif lf.type == 'member_expression':
                    key = _t(lf).replace('.prototype.', '.')
                    obj, prop = lf.child_by_field_name('object'), _t(lf.child_by_field_name('property'))
                    if right.type in FUNC:
                        defs[key] = line
                    if _t(obj) in ('exports', 'module.exports'):
                        exports[prop] = key if right.type in FUNC else (
                            _t(right) if right.type == 'identifier' else key)
        elif t == 'import_statement':
            spec = _str(n.child_by_field_name('source'))
            for c in n.named_children:
                if c.type == 'import_clause' and spec:
                    for x in c.named_children:
                        if x.type == 'identifier':
                            imports[_t(x)] = (spec, 'default')
                        elif x.type == 'namespace_import':
                            imports[_t(x.named_children[-1])] = (spec, None)
                        elif x.type == 'named_imports':
                            for s in x.named_children:
                                nm, al = s.child_by_field_name('name'), s.child_by_field_name('alias')
                                imports[_t(al or nm)] = (spec, _t(nm))
        elif t == 'export_statement':
            decl = n.child_by_field_name('declaration')
            is_default = any(c.type == 'default' for c in n.children)
            if decl is not None:
                if decl.type in ('function_declaration', 'class_declaration', 'generator_function_declaration'):
                    nm = _t(decl.child_by_field_name('name'))
                    exports['default' if is_default else nm] = nm
                elif decl.type in ('lexical_declaration', 'variable_declaration'):
                    for d in decl.named_children:
                        if d.type == 'variable_declarator':
                            exports[_t(d.child_by_field_name('name'))] = _t(d.child_by_field_name('name'))
            elif is_default:
                val = n.child_by_field_name('value') or (n.named_children[-1] if n.named_children else None)
                if val is not None and val.type == 'identifier':
                    exports['default'] = _t(val)
            for c in n.named_children:
                if c.type == 'export_clause':
                    for s in c.named_children:
                        nm, al = s.child_by_field_name('name'), s.child_by_field_name('alias')
                        exports[_t(al or nm)] = _t(nm)
        elif t == 'call_expression':
            fn = n.child_by_field_name('function')
            if fn is not None:
                if fn.type == 'identifier' and _t(fn) != 'require':
                    calls.append((line, 'bare', None, _t(fn), line))
                elif fn.type == 'member_expression':
                    obj, prop = fn.child_by_field_name('object'), fn.child_by_field_name('property')
                    r = _require_of(obj)
                    pl = prop.start_point[0] + 1 if prop is not None else line
                    if r and r[1] is None:
                        calls.append((line, 'inline', r[0], _t(prop), pl))
                    elif obj is not None and obj.type == 'identifier':
                        calls.append((line, 'member', _t(obj), _t(prop), pl))
        for c in n.children:
            walk(c)

    walk(tree.root_node)
    for obj in export_obj:                     # module.exports = X  ->  X.f are exports, X itself callable
        if obj in defs and default_fn is None:
            default_fn = obj
        for k in list(defs):
            if k.startswith(obj + '.') and k.count('.') == 1:
                exports.setdefault(k.split('.', 1)[1], k)
    if default_fn:
        exports.setdefault('default', default_fn)
    return {'defs': defs, 'imports': imports, 'exports': exports, 'calls': calls}


def _resolve(spec, from_file, fileset):
    if not spec or not spec.startswith('.'):
        return None
    base = os.path.normpath(os.path.join(os.path.dirname(from_file), spec))
    for e in EXTS:
        if base + e in fileset:
            return base + e
    return None


def _export_line(P, fileset, tf, name, seen=0):
    """(file, line) of the definition that file tf exports as `name`; follows re-exports of imports."""
    p = P.get(tf)
    if p is None or seen > 3:
        return None
    key = p['exports'].get(name)
    if key is None:
        return None
    if key in p['defs']:
        return tf, p['defs'][key]
    if key in p['imports']:
        spec, member = p['imports'][key]
        nf = _resolve(spec, tf, fileset)
        return _export_line(P, fileset, nf, member or 'default', seen + 1) if nf else None
    return None


def _base(qn):
    """`file::C.m:S1a2b` -> `file::C.m` (CRG's suffix for a second definition with the same name)."""
    return re.sub(r':S[0-9a-f]+$', '', qn)


def _short(target):
    tail = target.rsplit('::', 1)[-1]
    return tail.split(':', 1)[0].rsplit('.', 1)[-1]


def resolve_js_requires(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'files': 0, 'certain_calls': 0, 'calls_resolved': 0,
             'already_bound': 0, 'no_crg_edge': 0, 'no_target_node': 0}
    if not stats['enabled']:
        return stats
    files = [f for f in store.get_all_files() if f.lower().endswith(JS_EXT)]
    if not files:
        return stats
    from tree_sitter_language_pack import get_parser
    parser = get_parser('javascript')
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    fileset = set(files)
    P = {}
    for f in files:
        try:
            with open(f, 'rb') as fh:
                P[f] = _parse(fh.read(), parser)
        except (OSError, ValueError) as exc:
            logger.warning('js resolver: cannot parse %s: %s', f, exc)
    stats['files'] = len(P)

    node_at = defaultdict(list)
    for row in conn.execute("SELECT name, qualified_name, file_path, line_start FROM nodes "
                            "WHERE kind IN ('Function','Class','Test')"):
        if row['file_path'] in fileset:
            node_at[(row['file_path'], row['line_start'])].append((row['name'], row['qualified_name']))
    node_pos = {r[0]: (r[1], r[2]) for r in conn.execute('SELECT qualified_name, file_path, line_start FROM nodes')}
    node_names = set(node_pos)
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        if row['file_path'] in fileset:
            edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))

    # pass 1: every syntax-certain call with its one target node
    certain = []
    for f, p in P.items():
        for line, kind, recv, name, pline in p['calls']:
            if kind == 'bare' and name in p['imports']:
                spec, imp = p['imports'][name]
                member = imp or 'default'
            elif kind == 'member' and recv in p['imports']:
                spec, imp = p['imports'][recv]
                if imp not in (None, 'default'):
                    continue
                member = name
            elif kind == 'inline':
                spec, member = recv, name
            else:
                continue
            tf = _resolve(spec, f, fileset)
            if not tf or tf == f:
                continue
            got = _export_line(P, fileset, tf, member)
            if not got:
                continue
            stats['certain_calls'] += 1
            nodes = node_at.get(got, [])
            if len(nodes) > 1:
                # CRG can store one function as several nodes starting on the same line (`module.exports` and
                # `exports`; `exports.listen` and `listen(0)`). They are the same function; pick one by name.
                want = member.rsplit('.', 1)[-1]
                nodes = ([nd for nd in nodes if nd[0] == want]
                         or [nd for nd in nodes if nd[0].endswith('.' + want)]
                         or [nd for nd in nodes if re.sub(r'\(\d+\)$', '', nd[0]) == want]
                         or [nd for nd in nodes if nd[0] == 'module.exports'] or sorted(nodes))[:1]
            if not nodes:
                stats['no_target_node'] += 1
                continue
            certain.append((f, line, pline, name, nodes[0][1], spec))

    # targets that some certain call on a line points at: a CRG edge to one of these is never "wrong"
    cert_targets = defaultdict(set)
    for f, line, pline, name, target, spec in certain:
        cert_targets[(f, line)].add(target)
        cert_targets[(f, pline)].add(target)
    fix_wrong = _fix_wrong_enabled()
    stats['wrong_fixed'] = 0

    # pass 2: rewrite the CRG edge of each certain call
    updates = []
    for f, line, pline, name, target, spec in certain:
        cands = [e for ln in dict.fromkeys((line, pline)) for e in edges_at.get((f, ln), [])
                 if _short(e[1]) == name]
        if not cands:
            stats['no_crg_edge'] += 1
            continue
        if any(e[1] == target and 'collision_endpoint' not in (e[2] or '') for e in cands):
            stats['already_bound'] += 1
            continue
        others = cert_targets[(f, line)] | cert_targets[(f, pline)]
        # wrong = another real node that is not a same-name duplicate and not an alias of the same function
        # (a node starting on the target's own line)
        is_wrong = lambda tq: (tq in node_names and _base(tq) != _base(target)  # noqa: E731
                               and node_pos.get(tq) != node_pos.get(target))
        # with Iteration 2 on, a wrong CRG binding on this call is corrected first; otherwise a bare edge
        cands.sort(key=lambda e: 0 if (fix_wrong and is_wrong(e[1]) and e[1] not in others) else 1)
        for eid, tq, extra in cands:
            rule = RULE
            if is_wrong(tq):
                # CRG bound it to another real node. Iteration 1 leaves that alone; Iteration 2
                # (js.fix-wrong) corrects it, unless that node is the certain target of another call
                # with the same name on the same line (`a.f(b.f())`).
                if not fix_wrong or tq in others:
                    continue
                rule = FIX_RULE
            d = json.loads(extra or '{}') or {}
            for k in ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count',
                      'ambiguous_targets_truncated', 'unresolved_targets', 'collision_endpoint'):
                d.pop(k, None)
            d.update({'rule': rule, 'js_import': spec})
            if rule == FIX_RULE:
                d.update({'collision_evidence': FIX_RULE, 'replaced_target': tq})
                stats['wrong_fixed'] += 1
            updates.append((target, json.dumps(d), eid))
            break

    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('JS require resolution: %s', stats)
    return stats
