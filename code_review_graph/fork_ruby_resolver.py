"""Post-build pass that binds Ruby calls whose target is fixed by syntax and the repo's own declarations
(our fork; notebooks/21-ruby).

What stock CRG does with Ruby (notebooks/21-ruby): ``Devise.mappings`` inside the library, ``Foo.new``,
and a receiverless call to a method defined in an ``include``d concern in another file mostly stay bare
UNRESOLVED names, because CRG's binding rule ("same file or exactly one imported file") does not know Ruby's
constant lookup, ``require`` paths that Zeitwerk/autoload make unnecessary, or mixins.

What this pass does, for .rb files only (the same strict definition of *certain* as lib/ruby_callers_ref.py):

1. ``Const.m`` / ``A::B.m``: the constant is resolved through the call's lexical scopes against the repo's own
   ``class``/``module`` declarations (a class must be declared in exactly one file); it must have exactly one
   class method ``m`` (``def self.m`` or ``def m`` in ``class << self``).
2. ``Const.new``: the class (one file, no ``def self.new``) has exactly one ``initialize``; target = initialize.
3. ``m(...)`` / ``self.m(...)`` inside a method: ``m`` is defined once in the whole repo (for that kind), in the
   caller's class/module or its include/superclass ancestry (no ``prepend``); not inside DSL / ``*_eval`` blocks.
Only cross-file targets. The CRG CALLS edge on the call's line (or the method name's line) with that name
(``new`` for constructors) is rewritten to the CRG node at the definition's line when its target is not a real
node (bare / AMBIGUOUS), or a same-name duplicate of it (``m`` vs ``m:S<hash>``). Nothing is invented.
Rewritten edges lose ``claimed_endpoint``, ambiguous lists and ``collision_endpoint``, and get
``extra.rule = 'ruby.require-resolver'`` and ``extra.ruby_via``.

Iteration 2, rule ``ruby.fix-wrong`` (on by default; off with ``CRG_RULES_OFF=ruby.fix-wrong``): when the target is
certain and CRG bound the edge to a DIFFERENT real node, the edge is rewritten too (only when every call with
that name on the line is a certain one; otherwise the edge may belong to the other call and is kept) (``extra.rule =
'ruby.fix-wrong'``, ``ruby_replaced``, ``collision_evidence``), so CRG's collision pass keeps it
(``crg.keep-resolver-evidence`` in graph.py).

Switch: ``CRG_RULES=off``, ``CRG_RULES_OFF=ruby.require-resolver``, or ``CRG_RULES_ONLY``.
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

RULE = 'ruby.require-resolver'
FIX_RULE = 'ruby.fix-wrong'        # Iteration 2
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


# ---- shared Ruby parse + index (identical copy in lib/ruby_callers_ref.py and fork_ruby_resolver.py) ----
SCOPES = ('class', 'module')
METHODS = ('method', 'singleton_method')
# blocks whose body may run with a different self (DSLs, *_eval, define_method): receiverless calls inside are skipped
DSL_BLOCKS = frozenset('''instance_eval instance_exec class_eval class_exec module_eval module_exec define_method
define_singleton_method configure draw included prepended extended class_methods inherited new tap then
yield_self with_options concern routes namespace scope'''.split())


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _const_path(n):
    """constant / scope_resolution -> (parts, absolute) or None."""
    if n is None:
        return None
    if n.type == 'constant':
        return [_t(n)], False
    if n.type == 'scope_resolution':
        name, scope = n.child_by_field_name('name'), n.child_by_field_name('scope')
        if name is None or name.type != 'constant':
            return None
        if scope is None:
            return [_t(name)], True                      # ::Foo
        inner = _const_path(scope)
        if inner is None:
            return None
        return inner[0] + [_t(name)], inner[1]
    return None


def parse(src, parser):
    """One Ruby file -> declarations, method defs, includes and call sites (with their lexical constant scope)."""
    root = parser.parse(src).root_node
    decls, defs, incs, calls = [], [], [], []
    names = {}                                # (line, method name) -> number of call nodes with that name there
    # stack frames: (node, lexical chain [innermost full name first], owner full name or None,
    #                method context None|'i'|'s', in_singleton_class, in_dsl_block)
    stack = [(root, (), None, None, False, False)]
    while stack:
        n, chain, owner, mctx, sclass, dsl = stack.pop()
        t = n.type
        kids_ctx = (chain, owner, mctx, sclass, dsl)
        if t in SCOPES:
            cp = _const_path(n.child_by_field_name('name'))
            if cp is not None:
                parts, absolute = cp
                full = '::'.join(parts) if (absolute or not chain) else chain[0] + '::' + '::'.join(parts)
                sup = None
                if t == 'class':
                    s = n.child_by_field_name('superclass')
                    if s is not None and s.named_children:
                        sup = _const_path(s.named_children[0])
                decls.append({'full': full, 'kind': t, 'line': n.start_point[0] + 1, 'super': sup, 'chain': chain})
                kids_ctx = ((full,) + chain, full, None, False, False)
            else:
                kids_ctx = (chain, None, None, False, dsl)
        elif t == 'singleton_class':
            obj = n.named_children[0] if n.named_children else None
            kids_ctx = (chain, owner if obj is not None and obj.type == 'self' else None, None, True, dsl)
        elif t in METHODS:
            name = n.child_by_field_name('name')
            nm = _t(name)
            if t == 'singleton_method':
                obj = n.child_by_field_name('object')
                kind = 's' if obj is not None and obj.type == 'self' else None
            else:
                kind = 's' if sclass else 'i'
            if owner and kind and nm:
                defs.append({'owner': owner, 'kind': kind, 'name': nm, 'line': n.start_point[0] + 1})
            kids_ctx = (chain, owner if kind else None, kind, False, False)
        elif t == 'call':
            meth, recv = n.child_by_field_name('method'), n.child_by_field_name('receiver')
            nm = _t(meth)
            line = n.start_point[0] + 1
            pline = meth.start_point[0] + 1 if meth is not None else line
            blk = n.child_by_field_name('block')
            for ln in {line, pline}:
                names[(ln, nm)] = names.get((ln, nm), 0) + 1
            if recv is None and nm in ('include', 'prepend') and owner and mctx is None:
                args = n.child_by_field_name('arguments')
                for a in (args.named_children if args is not None else []):
                    cp = _const_path(a)
                    if cp is not None:
                        incs.append({'owner': owner, 'kind': nm, 'path': cp, 'chain': chain})
            if meth is not None and meth.type in ('identifier', 'constant'):
                cp = _const_path(recv) if recv is not None else None
                if cp is not None:
                    calls.append({'line': line, 'pline': pline, 'kind': 'const', 'path': cp, 'chain': chain,
                                  'name': nm})
                elif (recv is None or recv.type == 'self') and owner and mctx and not dsl:
                    calls.append({'line': line, 'pline': pline, 'kind': 'self', 'owner': owner, 'ctx': mctx,
                                  'name': nm})
            if blk is not None:
                # the block runs with another self when it belongs to a DSL / *_eval call; mark its subtree
                inner_dsl = dsl or nm in DSL_BLOCKS
                for c in n.named_children:
                    stack.append((c, chain, owner, mctx, sclass, inner_dsl if c == blk else dsl))
                continue
        elif t in ('lambda',):
            pass
        for c in n.named_children:
            stack.append((c,) + kids_ctx)
    return {'decls': decls, 'defs': defs, 'incs': incs, 'calls': calls, 'names': names}


class Index:
    """Repo-wide constants, method definitions and ancestry, built from every parsed file."""

    def __init__(self, P):
        self.decl_files = {}                  # full name -> set of files declaring it
        self.kind = {}                        # full name -> 'class' | 'module' (mixed -> None)
        self.supers = {}                      # class full name -> set of (path, chain)
        self.incs = {}                        # full name -> list of (path, chain)
        self.sdefs, self.idefs = {}, {}       # (owner, name) -> [(file, line)]
        self.iname = {}                       # instance method name -> [(owner, file, line)] repo-wide
        self.sname = {}
        for f, p in P.items():
            for d in p['decls']:
                self.decl_files.setdefault(d['full'], set()).add(f)
                k = self.kind.get(d['full'], d['kind'])
                self.kind[d['full']] = k if k == d['kind'] else None
                if d['super'] is not None:
                    self.supers.setdefault(d['full'], set()).add((tuple(d['super'][0]), d['super'][1], d['chain']))
            for i in p['incs']:
                if i['kind'] == 'include':
                    self.incs.setdefault(i['owner'], []).append((i['path'], i['chain']))
                else:
                    self.incs.setdefault(i['owner'], []).append(('PREPEND', None))
            for d in p['defs']:
                tbl, by = (self.sdefs, self.sname) if d['kind'] == 's' else (self.idefs, self.iname)
                tbl.setdefault((d['owner'], d['name']), []).append((f, d['line']))
                by.setdefault(d['name'], []).append((d['owner'], f, d['line']))

    def resolve(self, path, chain):
        """Ruby constant lookup through the lexical scopes, repo constants only. Full name or None."""
        parts, absolute = path
        rel = '::'.join(parts)
        cands = [rel] if absolute else [c + '::' + rel for c in chain] + [rel]
        for c in cands:
            if c in self.decl_files:
                return c
        return None

    def certain_class(self, full):
        """A class counts only if it is declared in exactly one file (reopenings or same-name test doubles: no)."""
        return self.kind.get(full) == 'class' and len(self.decl_files.get(full, ())) == 1

    def ancestors(self, full, depth=0):
        """[full, its includes..., superclass ancestors...]; None if a step is unknown or a prepend is involved."""
        if depth > 8:
            return None
        out = [full]
        for path, chain in self.incs.get(full, []):
            if path == 'PREPEND':
                return None
            r = self.resolve(path, chain)
            if r is None:
                continue                           # external module: cannot define a repo method
            sub = self.ancestors(r, depth + 1)
            if sub is None:
                return None
            out += sub
        sups = self.supers.get(full, set())
        if len(sups) > 1:
            return None
        for parts, absolute, chain in sups:
            r = self.resolve((list(parts), absolute), chain)
            if r is not None and r != full:
                sub = self.ancestors(r, depth + 1)
                if sub is None:
                    return None
                out += sub
        return out

    def target(self, call):
        """(file, line, how) of the one repo definition the call must reach, or None if not certain."""
        if call['kind'] == 'const':
            full = self.resolve(call['path'], call['chain'])
            if full is None:
                return None
            if call['name'] == 'new':
                if not self.certain_class(full) or self.sdefs.get((full, 'new')):
                    return None                          # a class with its own `def self.new` does not go to initialize
                d = self.idefs.get((full, 'initialize'), [])
                return (d[0][0], d[0][1], 'Const.new -> initialize') if len(d) == 1 else None
            if self.kind.get(full) is None:
                return None
            if self.kind[full] == 'class' and not self.certain_class(full):
                return None
            d = self.sdefs.get((full, call['name']), [])
            return (d[0][0], d[0][1], 'Const.method') if len(d) == 1 else None
        # receiverless / self.m inside a method: the name must be defined exactly once in the whole repo (for that
        # kind), and that one definition must be in the caller's own class/module or its ancestry
        by = self.sname if call['ctx'] == 's' else self.iname
        d = by.get(call['name'], [])
        if len(d) != 1:
            return None
        owner, f, ln = d[0]
        if call['ctx'] == 's':
            return (f, ln, 'self.method (class method)') if owner == call['owner'] else None
        anc = self.ancestors(call['owner'])
        if anc is None or owner not in anc:
            return None
        return (f, ln, 'self / receiverless method')
# ---- end shared ----


def _base(qn):
    return re.sub(r':S[0-9a-f]+$', '', qn)


def _short(t):
    return t.rsplit('::', 1)[-1].split(':S', 1)[0].rsplit('.', 1)[-1]


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'files': 0, 'certain_calls': 0, 'calls_resolved': 0, 'already_bound': 0,
             'no_crg_edge': 0, 'no_target_node': 0, 'other_binding_kept': 0, 'fix_wrong_corrected': 0}
    if not stats['enabled']:
        return stats
    from .rules import switch_on
    fix_wrong = switch_on(FIX_RULE)
    stats['fix_wrong_enabled'] = fix_wrong
    files = [f for f in store.get_all_files() if f.endswith('.rb')]
    if not files:
        return stats
    from tree_sitter_language_pack import get_parser
    parser = get_parser('ruby')
    P = {}
    for f in files:
        try:
            with open(f, 'rb') as fh:
                P[f] = parse(fh.read(), parser)
        except (OSError, ValueError) as exc:
            logger.warning('ruby resolver: cannot parse %s: %s', f, exc)
    stats['files'] = len(P)
    index = Index(P)
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    fileset = set(P)
    node_at = defaultdict(list)
    real = set()
    for row in conn.execute("SELECT qualified_name, name, file_path, line_start, kind FROM nodes"):
        real.add(row['qualified_name'])
        if row['kind'] in ('Function', 'Test') and row['file_path'] in fileset:
            node_at[(row['file_path'], row['line_start'])].append((row['name'], row['qualified_name']))
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        if row['file_path'] in fileset:
            edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))

    updates, done = [], set()
    for f, p in P.items():
        cert_n = defaultdict(int)                  # certain calls per (line, name) in this file
        for c in p['calls']:
            tg = index.target(c)
            if tg is not None and tg[0] != f:
                for ln in {c['line'], c['pline']}:
                    cert_n[(ln, c['name'])] += 1
        for c in p['calls']:
            tg = index.target(c)
            if tg is None or tg[0] == f:
                continue
            stats['certain_calls'] += 1
            want = 'initialize' if tg[2].startswith('Const.new') else c['name']
            nodes = node_at.get((tg[0], tg[1]), [])
            if len(nodes) > 1:
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
                rule = RULE
                if tq in real and _base(tq) != _base(target):
                    alone = all(p['names'].get((ln, c['name']), 0) <= cert_n[(ln, c['name'])]
                                for ln in {c['line'], c['pline']})
                    if not fix_wrong or not alone:   # another same-name call on the line may own this edge
                        stats['other_binding_kept'] += 1
                        continue
                    rule = FIX_RULE               # Iteration 2: syntax proves CRG's binding wrong
                d = json.loads(extra or '{}') or {}
                for k in DROP:
                    d.pop(k, None)
                new = {**d, 'rule': rule, 'ruby_via': tg[2]}
                if rule == FIX_RULE:
                    new['ruby_replaced'] = tq
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
    logger.info('Ruby require resolution: %s', stats)
    return stats
