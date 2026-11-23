"""Post-build pass that binds Go calls whose target is fixed by the package layout (our fork; notebooks/13-go).

What stock CRG does with Go (notebooks/13-go, fzf / caddy / terraform):

- ``F()`` calling a func defined in ANOTHER file of the same package stays a bare UNRESOLVED name. Go packages
  span files, so this is the most common cross-file call in Go, and CRG's rule ("same file or one imported
  file") never sees the sibling file.
- ``pkg.F()`` is usually bound, but becomes AMBIGUOUS when two files of the repo define ``F``.
- A bare ``F()`` can get bound to a method of another package (``Error(...)`` in caddyhttp ->
  ``admin.go::APIError.Error``), which Go makes impossible: a bare call names a func of the caller's own package.

What this pass does, on the stored graph, for .go files only:

1. Parse each file with tree-sitter: package clause, imports, top-level funcs and call sites.
   Files with ``//go:build ignore`` are left out. Import paths map to directories through every go.mod in the
   repo (longest module path wins).
2. A call is certain when
   - ``F()`` is not shadowed in the calling function and ``F`` is a top-level func defined exactly once in the
     caller's package (same directory and package clause), in another file; or
   - ``pkg.F()``: ``pkg`` is an import (alias or the package's own name) of a repo package that defines a
     top-level ``F`` exactly once, and ``pkg`` is not shadowed by a local name.
   A func defined twice in a package (build-tagged twins) is never certain.
3. The target is the CRG Function node at that func's line. The CRG CALLS edge on the call's line (or the
   selector's line) with that name is rewritten to it when its target is
   - not a real node (bare or AMBIGUOUS name), or
   - a same-name duplicate of it (``F`` vs ``F:S<hash>``), or
   - a node Go rules out: a method, or a func in another directory, for a bare ``F()``; a method for ``pkg.F()``.
     These are counted as ``wrong_bindings_corrected``.
   Any other existing binding is left alone. Nothing is invented: a call with no CRG edge stays without one.
   ``claimed_endpoint``, ambiguous lists and ``collision_endpoint`` are dropped from a rewritten edge, so CRG's
   later collision pass does not turn it back into AMBIGUOUS.

Iteration 2, rule ``go.fix-wrong`` (on by default; off with ``CRG_RULES_OFF=go.fix-wrong``): when the target is
certain and CRG bound the edge to a DIFFERENT real node that Go does not strictly rule out (e.g. a same-package
func of another name's duplicate, a func in the caller's own file), the edge is still rewritten, because the
package layout alone fixes the target. Such edges get ``extra.rule = 'go.fix-wrong'``, ``go_replaced`` and
``collision_evidence``, so CRG's collision pass keeps them (``crg.keep-resolver-evidence`` in graph.py).

Every rewritten edge gets ``extra.rule = 'go.import-resolver'`` and ``extra.go_via`` (the import path, or
``same package``). Switch: ``CRG_RULES=off``, ``CRG_RULES_OFF=go.import-resolver``, or ``CRG_RULES_ONLY``.
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

RULE = 'go.import-resolver'
FIX_RULE = 'go.fix-wrong'          # Iteration 2
FUNCS = ('function_declaration', 'method_declaration', 'func_literal')
IGNORE_TAG = re.compile(rb'^//go:build\s+ignore\b', re.M)
DROP = ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count', 'ambiguous_targets_truncated',
        'unresolved_targets', 'collision_endpoint')


def enabled() -> bool:
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return False
    names = lambda v: {s.strip() for s in os.environ.get(v, '').split(',') if s.strip()}  # noqa: E731
    only, off = names('CRG_RULES_ONLY'), names('CRG_RULES_OFF')
    if only:
        return RULE in only
    return RULE not in off


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _modules(files):
    """module path -> dir, from the go.mod files that sit above the indexed .go files."""
    dirs = set()
    for f in files:
        d = os.path.dirname(f)
        while d and d not in dirs:
            dirs.add(d)
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    out = {}
    for d in dirs:
        gm = os.path.join(d, 'go.mod')
        if os.path.isfile(gm):
            try:
                text = Path(gm).read_text(errors='replace')
            except OSError:
                continue
            m = re.search(r'^\s*module\s+(\S+)', text, re.M)
            if m:
                out[m.group(1).strip('"')] = d
    return out


def _import_dir(ip, mods):
    best = None
    for mp, d in mods.items():
        if (ip == mp or ip.startswith(mp + '/')) and (best is None or len(mp) > len(best[0])):
            best = (mp, d)
    if best is None:
        return None
    return os.path.normpath(os.path.join(best[1], ip[len(best[0]):].lstrip('/')))


def _declared_in(fn):
    names = set()
    stack = [fn]
    while stack:
        n = stack.pop()
        t = n.type
        if t in ('parameter_declaration', 'variadic_parameter_declaration'):
            names.update(_t(c) for c in n.children if c.type == 'identifier')
        elif t in ('short_var_declaration', 'range_clause', 'receive_statement'):
            left = n.child_by_field_name('left')
            if left is not None:
                names.update(_t(c) for c in ([left] if left.type == 'identifier' else left.named_children)
                             if c.type == 'identifier')
        elif t in ('var_spec', 'const_spec'):
            names.update(_t(c) for c in n.children if c.type == 'identifier')
        elif t == 'type_switch_statement':
            a = n.child_by_field_name('alias')
            if a is not None:
                names.update(_t(c) for c in ([a] if a.type == 'identifier' else a.named_children))
        stack.extend(n.children)
    return names


def _parse(src, parser):
    root = parser.parse(src).root_node
    pkg, imports, funcs, calls = None, {}, {}, []
    for c in root.named_children:
        if c.type == 'package_clause':
            pkg = _t(c.named_children[0]) if c.named_children else None
        elif c.type == 'import_declaration':
            specs = [s for s in c.named_children if s.type == 'import_spec']
            for lst in c.named_children:
                if lst.type == 'import_spec_list':
                    specs += [s for s in lst.named_children if s.type == 'import_spec']
            for s in specs:
                name, path_n = s.child_by_field_name('name'), s.child_by_field_name('path')
                if name is not None and _t(name) in ('.', '_'):
                    continue
                imports[_t(path_n).strip('"`')] = _t(name) if name is not None else None
        elif c.type == 'function_declaration':
            funcs.setdefault(_t(c.child_by_field_name('name')), []).append(c.start_point[0] + 1)
    cache = {}

    def shadowed(n, name):
        p = n.parent
        while p is not None:
            if p.type in FUNCS:
                k = (p.start_byte, p.end_byte)
                if k not in cache:
                    cache[k] = _declared_in(p)
                if name in cache[k]:
                    return True
            p = p.parent
        return False

    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == 'call_expression':
            fn = n.child_by_field_name('function')
            line = n.start_point[0] + 1
            if fn is not None and fn.type == 'identifier':
                if not shadowed(n, _t(fn)):
                    calls.append((line, line, None, _t(fn)))
            elif fn is not None and fn.type == 'selector_expression':
                op, fld = fn.child_by_field_name('operand'), fn.child_by_field_name('field')
                if op is not None and op.type == 'identifier' and fld is not None and not shadowed(n, _t(op)):
                    calls.append((line, fld.start_point[0] + 1, _t(op), _t(fld)))
        stack.extend(n.children)
    return {'pkg': pkg, 'imports': imports, 'funcs': funcs, 'calls': calls,
            'ignored': bool(IGNORE_TAG.search(src[:2000]))}


def _base(qn):
    return re.sub(r':S[0-9a-f]+$', '', qn)


def _short(target):
    return target.rsplit('::', 1)[-1].split(':S', 1)[0].rsplit('.', 1)[-1]


def _is_method(qn):
    return '.' in qn.rsplit('::', 1)[-1].split(':S', 1)[0]


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'files': 0, 'certain_calls': 0, 'calls_resolved': 0,
             'wrong_bindings_corrected': 0, 'already_bound': 0, 'no_crg_edge': 0, 'no_target_node': 0,
             'other_binding_kept': 0, 'fix_wrong_corrected': 0}
    if not stats['enabled']:
        return stats
    from .rules import switch_on
    fix_wrong = switch_on(FIX_RULE)
    stats['fix_wrong_enabled'] = fix_wrong
    files = [f for f in store.get_all_files() if f.endswith('.go')]
    if not files:
        return stats
    from tree_sitter_language_pack import get_parser
    parser = get_parser('go')
    P = {}
    for f in files:
        try:
            with open(f, 'rb') as fh:
                p = _parse(fh.read(), parser)
        except (OSError, ValueError) as exc:
            logger.warning('go resolver: cannot parse %s: %s', f, exc)
            continue
        if p['pkg'] and not p['ignored']:
            P[f] = p
    stats['files'] = len(P)
    mods = _modules(list(P))
    pkg_funcs = defaultdict(lambda: defaultdict(list))
    dir_count = defaultdict(lambda: defaultdict(int))
    for f, p in P.items():
        d = os.path.dirname(f)
        for nm, lines in p['funcs'].items():
            pkg_funcs[(d, p['pkg'])][nm].extend((f, ln) for ln in lines)
        if not p['pkg'].endswith('_test'):
            dir_count[d][p['pkg']] += 0 if f.endswith('_test.go') else 1
    dir_pkg = {d: max(c.items(), key=lambda kv: kv[1])[0] for d, c in dir_count.items() if c}

    conn = store._conn  # post-build maintenance pass, like the other resolvers
    fileset = set(P)
    node_at = defaultdict(list)
    node_file = {}
    for row in conn.execute("SELECT qualified_name, file_path, line_start, kind FROM nodes"):
        node_file[row['qualified_name']] = row['file_path']
        if row['kind'] in ('Function', 'Test') and row['file_path'] in fileset:
            node_at[(row['file_path'], row['line_start'])].append(row['qualified_name'])
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        if row['file_path'] in fileset:
            edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))

    updates, done = [], set()
    for f, p in P.items():
        d = os.path.dirname(f)
        local = {}
        for ip, alias in p['imports'].items():
            td = _import_dir(ip, mods)
            if td and td in dir_pkg:
                local[alias or dir_pkg[td]] = (td, dir_pkg[td], ip)
        for line, pline, recv, name in p['calls']:
            if recv is None:
                defs = pkg_funcs[(d, p['pkg'])].get(name, [])
                if len(defs) != 1 or defs[0][0] == f:
                    continue
                via = 'same package'
            elif recv in local:
                td, tp, via = local[recv]
                defs = pkg_funcs[(td, tp)].get(name, [])
                if len(defs) != 1:
                    continue
            else:
                continue
            stats['certain_calls'] += 1
            nodes = node_at.get(defs[0], [])
            if len(nodes) > 1:
                nodes = [q for q in nodes if _short(q) == name] or nodes
            if len(nodes) != 1:
                stats['no_target_node'] += 1
                continue
            target = nodes[0]
            cands = [e for ln in dict.fromkeys((line, pline)) for e in edges_at.get((f, ln), [])
                     if _short(e[1]) == name and e[0] not in done]
            if not cands:
                stats['no_crg_edge'] += 1
                continue
            if any(e[1] == target and 'collision_endpoint' not in (e[2] or '') for e in cands):
                stats['already_bound'] += 1
                continue
            for eid, tq, extra in cands:
                corrected, rule = False, RULE
                if tq in node_file and _base(tq) != _base(target):
                    impossible = _is_method(tq) or (recv is None and os.path.dirname(node_file[tq]) != d)
                    if not impossible:
                        if not fix_wrong:
                            stats['other_binding_kept'] += 1
                            continue
                        rule = FIX_RULE           # Iteration 2: the package layout proves this binding wrong
                    corrected = True
                dd = json.loads(extra or '{}') or {}
                for k in DROP:
                    dd.pop(k, None)
                if corrected:
                    dd['go_replaced'] = tq
                extra_new = {**dd, 'rule': rule, 'go_via': via}
                if rule == FIX_RULE:
                    extra_new['collision_evidence'] = FIX_RULE
                    stats['fix_wrong_corrected'] += 1
                else:
                    stats['wrong_bindings_corrected'] += corrected
                updates.append((target, json.dumps(extra_new), eid))
                done.add(eid)
                break

    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('Go import resolution: %s', stats)
    return stats
