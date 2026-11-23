"""Post-build pass that binds Swift calls whose target is fixed by module visibility (our fork; notebooks/22-swift).

Swift has no per-file imports inside a module: every file of a module sees every top-level declaration of that
module, and ``import X`` makes another module's declarations visible. Stock CRG binds a call only when the target
is in the same file or in exactly one imported *file*; Swift imports name modules, not files, so cross-file calls
stay bare names (notes/11 C1: swift linked 0 cross-file edges in the survey).

Modules (how this pass decides which files can see which declarations):
- Every ``Package.swift`` in the repo is read for ``.target`` / ``.executableTarget`` / ``.testTarget`` /
  ``.macro`` / ``.plugin`` declarations (``name:`` and optional ``path:``; default path ``Sources/<name>``,
  ``Tests/<name>`` for test targets, ``Plugins/<name>`` for plugins). A file belongs to the target whose
  directory is the longest prefix of its path.
- Files under no SwiftPM target (Xcode app targets, examples) are put in ONE module named ``app``. This is an
  approximation (an Xcode project can hold several targets); it is conservative, because a name must then be
  unique across all those files together before a call counts as certain.
- A file sees its own module plus every repo module it names in an ``import`` line.

A call is *certain* (from syntax alone) when, among the declarations visible to the calling file:
- ``f(a:b:)``: exactly one top-level ``func f`` accepts the call's argument labels (defaults and trailing
  closures allowed), it is in another file, ``f`` is not a local name in the calling function, and - when the
  call is inside a type or extension - no type of the visible modules has a member named ``f`` (an implicit
  ``self.f()`` would win);
- ``T(...)`` / ``T.init(...)``: ``T`` is declared exactly once as a class/struct/enum/actor (extensions do not
  count), is not also a function name, and is not declared as a nested type anywhere in the visible modules
  (inside ``Outer``, ``Inner(...)`` means ``Outer.Inner``); the target is the type's node (Class) in another file.
  An edge already bound to that type's declaration, to one of its extensions or to one of its ``init``s is right;
- ``T.m(...)``: ``T`` is such a type (not nested, not a local name) and exactly one ``static``/``class`` func ``m`` of
  ``T`` (in its declaration or any extension of ``T`` in the visible modules) accepts the argument labels; the
  target is in another file.
Method calls on values (``x.m()``), ``self.m()``, nested types and calls inside macros are never counted.

What the pass does (rule ``swift.import-resolver``, Iteration 1): for each certain call, the CRG CALLS edge on the
call's line (or the member name's line) with that name is rewritten to the target node when its target is not a
real node (bare / AMBIGUOUS), or is a same-name duplicate (``f`` vs ``f:S<hash>``). Iteration 2 (rule
``swift.fix-wrong``): when CRG bound that edge to a DIFFERENT real node (e.g. ``ConstraintMaker.makeConstraints()``
bound to the caller's own ``ConstraintViewDSL.makeConstraints``), the edge is rewritten too, tagged
``swift.fix-wrong`` and ``collision_evidence``. Nothing is invented: a call with no CRG edge stays without one.
Rewritten edges lose ``claimed_endpoint``, ambiguous lists and ``collision_endpoint``.

Switches: ``CRG_RULES=off``; ``CRG_RULES_OFF=swift.import-resolver`` (and/or ``swift.fix-wrong``);
``CRG_RULES_ONLY``.
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

RULE = 'swift.import-resolver'
FIX_RULE = 'swift.fix-wrong'          # Iteration 2
DROP = ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count', 'ambiguous_targets_truncated',
        'unresolved_targets', 'collision_endpoint')
TYPE_KW = ('class', 'struct', 'enum', 'actor', 'extension')
_TARGET = re.compile(r'\.(target|executableTarget|testTarget|macro|plugin)\s*\(\s*name\s*:\s*"([^"]+)"')
_PATH = re.compile(r'\bpath\s*:\s*"([^"]+)"')


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


# ---------------------------------------------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------------------------------------------
def package_targets(root):
    """target dir -> module name, from every Package.swift under root (skips .git, .build, Pods)."""
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ('.git', '.build', 'Pods', 'node_modules', 'DerivedData')]
        if 'Package.swift' not in fns:
            continue
        try:
            text = open(os.path.join(dp, 'Package.swift'), encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        for m in _TARGET.finditer(text):
            kind, name = m.group(1), m.group(2)
            # the path argument, if any, sits in the same call before the next target declaration
            nxt = _TARGET.search(text, m.end())
            seg = text[m.end(): nxt.start() if nxt else len(text)]
            pm = _PATH.search(seg)
            if pm:
                rel = pm.group(1)
            else:
                rel = {'testTarget': 'Tests', 'plugin': 'Plugins'}.get(kind, 'Sources') + '/' + name
            d = os.path.normpath(os.path.join(dp, rel))
            if os.path.isdir(d):
                out[d] = name
    return out


def module_of(f, targets):
    best = None
    for d, name in targets.items():
        if f.startswith(d + os.sep) and (best is None or len(d) > len(best[0])):
            best = (d, name)
    return best[1] if best else 'app'


# ---------------------------------------------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------------------------------------------
def _decl_kind(n):
    for c in n.children:
        if not c.is_named and _t(c) in TYPE_KW:
            return _t(c)
    return None


def _decl_name(n):
    for c in n.children:
        if c.type == 'type_identifier':
            return _t(c)
        if c.type == 'user_type':                    # extension Foo / extension Foo.Bar
            ids = [x for x in c.children if x.type == 'type_identifier']
            return _t(ids[-1]) if len(ids) == 1 else None
    return None


def _is_static(fn):
    for c in fn.children:
        if c.type == 'modifiers':
            for m in c.children:
                if m.type == 'property_modifier' and _t(m) in ('static', 'class'):
                    return True
        if not c.is_named and _t(c) in ('static', 'class'):
            return True
    return False


def _params(fn):
    """[(external label or None, has_default, variadic)] for a function/init declaration."""
    out = []
    kids = fn.children
    for i, c in enumerate(kids):
        if c.type != 'parameter':
            continue
        ids = [x for x in c.children if x.type == 'simple_identifier']
        if not ids:
            continue
        lab = _t(ids[0])
        variadic = any(not x.is_named and _t(x) == '...' for x in c.children)
        # a default value follows the parameter as `= expr` sibling tokens
        has_default = i + 1 < len(kids) and not kids[i + 1].is_named and _t(kids[i + 1]) == '='
        out.append((None if lab == '_' else lab, has_default, variadic))
    return out


def _call_args(suffix):
    """(labels list, trailing closure count) from a call_suffix."""
    labels, trailing = [], 0
    for c in suffix.children:
        if c.type == 'value_arguments':
            for a in c.named_children:
                if a.type == 'value_argument':
                    lab = next((x for x in a.children if x.type == 'value_argument_label'), None)
                    labels.append(_t(lab) if lab is not None else None)
        elif c.type in ('lambda_literal', 'annotated_lambda'):
            trailing += 1
    return labels, trailing


def accepts(params, labels, trailing):
    """Can a declaration with these params take a call with these labels (+ trailing closures)?"""
    j = 0
    rest = []
    for i, (lab, dflt, var) in enumerate(params):
        if j < len(labels) and labels[j] == lab:
            j += 1
            if var:
                while j < len(labels) and labels[j] is None:
                    j += 1
        else:
            rest.append((lab, dflt))
            if j < len(labels) and not dflt:
                return False                       # a required param is skipped while labelled args remain
    if j != len(labels):
        return False
    required = sum(1 for lab, d in rest if not d)
    return required <= trailing <= len(rest) if trailing else required == 0


def _locals(fn):
    names = set()
    stack = [fn]
    while stack:
        n = stack.pop()
        if n.type == 'parameter':
            ids = [x for x in n.children if x.type == 'simple_identifier']
            if ids:
                names.add(_t(ids[-1]))
        elif n.type in ('pattern', 'lambda_parameter'):
            for x in n.children:
                if x.type == 'simple_identifier':
                    names.add(_t(x))
        stack.extend(n.children)
    return names


def parse_file(src: bytes, parser):
    tree = parser.parse(src)
    imports, types, funcs, members, calls, nested = set(), [], [], set(), [], set()

    def body_of(n):
        return next((c for c in n.children if c.type in ('class_body', 'enum_class_body')), None)

    for top in tree.root_node.children:
        if top.type == 'import_declaration':
            ident = next((c for c in top.children if c.type == 'identifier'), None)
            if ident is not None:
                imports.add(_t(ident).split('.')[0])
        elif top.type == 'function_declaration':
            nm = next((c for c in top.children if c.type == 'simple_identifier'), None)
            if nm is not None:
                funcs.append({'owner': None, 'name': _t(nm), 'line': top.start_point[0] + 1,
                              'params': _params(top), 'static': True})
        elif top.type == 'class_declaration':
            kind, name = _decl_kind(top), _decl_name(top)
            if not kind or not name:
                continue
            types.append({'name': name, 'kind': kind, 'line': top.start_point[0] + 1})
            body = body_of(top)
            for m in (body.children if body is not None else []):
                if m.type == 'function_declaration':
                    nm = next((c for c in m.children if c.type == 'simple_identifier'), None)
                    if nm is not None:
                        members.add(_t(nm))
                        funcs.append({'owner': name, 'name': _t(nm), 'line': m.start_point[0] + 1,
                                      'params': _params(m), 'static': _is_static(m)})
                elif m.type == 'init_declaration':
                    funcs.append({'owner': name, 'name': 'init', 'line': m.start_point[0] + 1,
                                  'params': _params(m), 'static': True})
                elif m.type == 'property_declaration':
                    for p in m.children:
                        if p.type == 'pattern':
                            for x in p.children:
                                if x.type == 'simple_identifier':
                                    members.add(_t(x))
                elif m.type == 'enum_entry':
                    for x in m.children:
                        if x.type == 'simple_identifier':
                            members.add(_t(x))

    # names of types declared inside other declarations (Outer.Inner): inside Outer, `Inner(...)` means the nested
    # type, so such a name is never certain at a call site
    stack = [(c, False) for c in tree.root_node.children]
    while stack:
        n, inside = stack.pop()
        if n.type in ('class_declaration', 'protocol_declaration'):
            if inside:
                nm = _decl_name(n) if n.type == 'class_declaration' else next(
                    (_t(c) for c in n.children if c.type == 'type_identifier'), None)
                if nm:
                    nested.add(nm)
            inside = True
        stack.extend((c, inside) for c in n.children)

    # call sites, with the enclosing function's local names and whether we are inside a type
    stack = [(tree.root_node, frozenset(), False)]
    while stack:
        n, loc, in_type = stack.pop()
        if n.type in ('function_declaration', 'init_declaration', 'lambda_literal', 'computed_property',
                      'computed_getter', 'computed_setter'):
            loc = loc | _locals(n)
        if n.type == 'class_declaration':
            in_type = True
        if n.type == 'call_expression':
            fn = n.children[0] if n.children else None
            suffix = next((c for c in n.children if c.type == 'call_suffix'), None)
            if fn is not None and suffix is not None:
                labels, trailing = _call_args(suffix)
                line = n.start_point[0] + 1
                if fn.type == 'simple_identifier':
                    calls.append({'line': line, 'pline': line, 'kind': 'bare', 'recv': None, 'name': _t(fn),
                                  'labels': labels, 'trailing': trailing, 'shadowed': _t(fn) in loc,
                                  'in_type': in_type})
                elif fn.type == 'navigation_expression':
                    target = fn.children[0] if fn.children else None
                    nav = next((c for c in fn.children if c.type == 'navigation_suffix'), None)
                    mem = next((c for c in nav.children if c.type == 'simple_identifier'), None) if nav else None
                    if target is not None and target.type == 'simple_identifier' and mem is not None:
                        calls.append({'line': line, 'pline': mem.start_point[0] + 1, 'kind': 'member',
                                      'recv': _t(target), 'name': _t(mem), 'labels': labels, 'trailing': trailing,
                                      'shadowed': _t(target) in loc, 'in_type': in_type})
        for c in n.children:
            stack.append((c, loc, in_type))
    return {'imports': imports, 'types': types, 'funcs': funcs, 'members': members, 'calls': calls,
            'nested': nested}


# ---------------------------------------------------------------------------------------------------------------
# certain calls
# ---------------------------------------------------------------------------------------------------------------
def certain_calls(P, root):
    """P: file -> parse. Returns [call dict + file, target_file, target_line, target_kind, via]."""
    targets = package_targets(root)
    mod = {f: module_of(f, targets) for f in P}
    modules = set(mod.values())
    top_funcs = defaultdict(lambda: defaultdict(list))     # module -> name -> [(file, line, params)]
    type_decl = defaultdict(lambda: defaultdict(list))     # module -> type name -> [(file, line)] (no extensions)
    type_funcs = defaultdict(lambda: defaultdict(list))    # module -> (type, name) -> [(file, line, params, static)]
    type_ext = defaultdict(lambda: defaultdict(list))      # module -> type name -> [(file, line)] (extensions)
    mod_members, mod_nested = defaultdict(set), defaultdict(set)
    for f, p in P.items():
        m = mod[f]
        mod_members[m] |= p['members']
        mod_nested[m] |= p['nested']
        for fn in p['funcs']:
            if fn['owner'] is None:
                top_funcs[m][fn['name']].append((f, fn['line'], fn['params']))
            else:
                type_funcs[m][(fn['owner'], fn['name'])].append((f, fn['line'], fn['params'], fn['static']))
        for t in p['types']:
            if t['kind'] != 'extension':
                type_decl[m][t['name']].append((f, t['line']))
            else:
                type_ext[m][t['name']].append((f, t['line']))
    out = []
    for f, p in P.items():
        vis = [mod[f]] + sorted(i for i in p['imports'] if i in modules and i != mod[f])
        members = set().union(*(mod_members[m] for m in vis))
        nested = set().union(*(mod_nested[m] for m in vis))

        def accept_type(name, decl):
            # T(...) names the type: its declaration, any extension of it, or one of its inits are all right
            acc = [(decl[0], decl[1], 'type')]
            acc += [(f2, ln, 'type') for m in vis for f2, ln in type_ext[m].get(name, [])]
            acc += [(d[0], d[1], 'function') for m in vis for d in type_funcs[m].get((name, 'init'), [])]
            return acc

        def tdecl(name):
            return [d for m in vis for d in type_decl[m].get(name, [])]

        def tfuncs(name):
            return [d for m in vis for d in top_funcs[m].get(name, [])]

        for c in p['calls']:
            if c['shadowed']:
                continue
            hit = None
            if c['kind'] == 'bare':
                fs = tfuncs(c['name'])
                ts = tdecl(c['name'])
                if ts and not fs:                                      # T(...) initializer
                    if len(ts) == 1 and c['name'][:1].isupper() and c['name'] not in nested:
                        hit = (ts[0][0], ts[0][1], 'type', 'initializer T(...)', accept_type(c['name'], ts[0]))
                elif fs and not ts:
                    if c['in_type'] and c['name'] in members:
                        continue                                       # implicit self.f() could win
                    ok = [d for d in fs if accepts(d[2], c['labels'], c['trailing'])]
                    if len(ok) == 1:
                        hit = (ok[0][0], ok[0][1], 'function', 'module function f(...)', [(ok[0][0], ok[0][1], 'function')])
            elif c['recv'][:1].isupper() and c['recv'] not in nested:
                ts = tdecl(c['recv'])
                if len(ts) != 1:
                    continue
                if c['name'] == 'init':
                    hit = (ts[0][0], ts[0][1], 'type', 'initializer T.init(...)', accept_type(c['recv'], ts[0]))
                else:
                    cands = [d for m in vis for d in type_funcs[m].get((c['recv'], c['name']), [])]
                    ok = [d for d in cands if d[3] and accepts(d[2], c['labels'], c['trailing'])]
                    if len(ok) == 1 and len([d for d in cands if accepts(d[2], c['labels'], c['trailing'])]) == 1:
                        hit = (ok[0][0], ok[0][1], 'function', 'static T.m(...)', [(ok[0][0], ok[0][1], 'function')])
            if hit and hit[0] != f:
                out.append({**c, 'file': f, 'target_file': hit[0], 'target_line': hit[1], 'target_kind': hit[2],
                            'via': hit[3], 'accept': hit[4], 'module': mod[f], 'target_module': mod[hit[0]]})
    return out, mod


def _base(qn):
    return re.sub(r':S[0-9a-f]+(:\d+)?$', '', qn)


def short(target):
    tail = target.rsplit('::', 1)[-1]
    tail = re.sub(r':S[0-9a-f]+(:\d+)?$', '', tail)
    return tail.rsplit('.', 1)[-1]


def edge_name(c):
    """The name CRG stores for this call's edge: the type for T(...) and T.init(...), else the called name."""
    if c['via'] == 'initializer T(...)':
        return c['name']
    if c['via'] == 'initializer T.init(...)':
        return c['recv']
    return c['name']


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'files': 0, 'certain_calls': 0, 'calls_resolved': 0, 'already_bound': 0,
             'no_crg_edge': 0, 'no_target_node': 0, 'other_binding_kept': 0, 'fix_wrong_corrected': 0}
    if not stats['enabled']:
        return stats
    from .rules import switch_on
    fix_wrong = switch_on(FIX_RULE)
    stats['fix_wrong_enabled'] = fix_wrong
    files = [f for f in store.get_all_files() if f.endswith('.swift')]
    if not files:
        return stats
    from tree_sitter_language_pack import get_parser
    parser = get_parser('swift')
    P = {}
    for f in files:
        try:
            with open(f, 'rb') as fh:
                P[f] = parse_file(fh.read(), parser)
        except (OSError, ValueError) as exc:
            logger.warning('swift resolver: cannot parse %s: %s', f, exc)
    stats['files'] = len(P)
    root = os.path.commonpath(list(P)) if len(P) > 1 else os.path.dirname(next(iter(P)))
    d = root                                     # the repository root: nearest folder above with .git
    while os.path.dirname(d) != d and not os.path.exists(os.path.join(d, '.git')):
        d = os.path.dirname(d)
    certain, _ = certain_calls(P, d if os.path.exists(os.path.join(d, '.git')) else root)

    conn = store._conn  # post-build maintenance pass, like the other resolvers
    fileset = set(P)
    node_at = defaultdict(list)
    node_names = set()
    for row in conn.execute('SELECT qualified_name, file_path, line_start, kind FROM nodes'):
        node_names.add(row['qualified_name'])
        if row['kind'] in ('Function', 'Test', 'Class') and row['file_path'] in fileset:
            node_at[(row['file_path'], row['line_start'], 'type' if row['kind'] == 'Class' else 'function')].append(
                row['qualified_name'])
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        if row['file_path'] in fileset:
            edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))

    updates, done = [], set()
    for c in certain:
        stats['certain_calls'] += 1
        nodes = node_at.get((c['target_file'], c['target_line'], c['target_kind']), [])
        want = c['recv'] if c['via'] == 'initializer T.init(...)' else c['name']
        if len(nodes) > 1:
            nodes = [q for q in nodes if short(q) == want and q == _base(q)] or \
                    [q for q in nodes if short(q) == want] or nodes
        if len(nodes) != 1:
            stats['no_target_node'] += 1
            continue
        target = nodes[0]
        nm = edge_name(c)
        cands = [e for ln in dict.fromkeys((c['line'], c['pline'])) for e in edges_at.get((c['file'], ln), [])
                 if short(e[1]) in (nm, c['name']) and e[0] not in done]
        if not cands:
            stats['no_crg_edge'] += 1
            continue
        acceptable = {target}
        for af, al, ak in c['accept']:
            acceptable.update(q for q in node_at.get((af, al, ak), []) if short(q) == want or short(q) == 'init')
        if any(e[1] in acceptable and 'collision_endpoint' not in (e[2] or '') for e in cands):
            stats['already_bound'] += 1
            continue
        for eid, tq, extra in cands:
            rule = RULE
            if tq in acceptable and 'collision_endpoint' not in (extra or ''):
                continue
            if tq in node_names and _base(tq) not in {_base(a) for a in acceptable}:
                if not fix_wrong:
                    stats['other_binding_kept'] += 1
                    continue
                rule = FIX_RULE                  # Iteration 2: module visibility proves this binding wrong
            d = json.loads(extra or '{}') or {}
            for k in DROP:
                d.pop(k, None)
            new = {**d, 'rule': rule, 'swift_via': c['via']}
            if rule == FIX_RULE:
                new['swift_replaced'] = tq
                new['collision_evidence'] = FIX_RULE
                stats['fix_wrong_corrected'] += 1
            updates.append((target, json.dumps(new), eid))
            done.add(eid)
            break

    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('Swift import resolution: %s', stats)
    return stats
