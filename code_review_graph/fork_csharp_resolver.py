"""Post-build pass that binds C# calls whose target is fixed by namespaces, usings and declared types (our fork).

Measured in notebooks/19-csharp: CRG's own C# resolution (csharp_resolver / scoped_resolver) already binds most
static `T.M()` calls, but leaves a call UNRESOLVED as soon as the method name is overloaded (even when the argument
count picks one overload), binds nothing through `partial` classes or inherited methods, and has no notion of a DI
interface with a single implementation or of extension methods.

Iteration 1, rule ``csharp.import-resolver``: for every call the C# rules make certain (see the CORE docstring in
lib/csharp_callers_ref.py: static `T.M()` matched by argument count, `partial` class parts, inherited methods with
fixed dispatch, `using static`, `base.M()`, typed receivers with no override, a DI interface with exactly one
concrete implementation, a single applicable extension method), the CRG CALLS edge on that line with that name is
rewritten to the target node when its target is not a real node (or is a same-name duplicate `C.M` / `C.M:S<hash>`).

Iteration 2, rule ``csharp.fix-wrong`` (with the shared ``crg.keep-resolver-evidence`` in graph.py): when CRG bound
such a certain call to a DIFFERENT real node, the edge is rewritten to the certain target; and when a call can
never reach repo code (`x.M()` on a declared non-repo type with no repo extension `M`, or `T.M()` on a non-repo
type name), a binding CRG made to a repo node is taken back: the edge becomes the bare name, marked
``unresolved_targets: []`` (CRG's own "checked, leave it" marker), so no later pass guesses it again.

Order: CRG's C# pass runs after this hook in the build and re-derives every C# call from ``csharp_raw_target``.
This pass therefore runs CRG's C# pass first itself, and stores ``csharp_raw_target`` of every edge it rewrites as
``csharp_raw_target_fork``, so CRG's later run skips those edges (it only touches edges that carry the raw target).

Nothing is invented: a call with no CRG edge stays without one (CRG stores no edge for `new T()`, so constructors
are never bound). Every rewritten edge gets ``extra.rule`` and ``extra.csharp_via``; Iteration 2 edges also carry
``collision_evidence``. Switches: ``CRG_RULES=off``, ``CRG_RULES_OFF=csharp.import-resolver`` (both iterations off),
``CRG_RULES_OFF=csharp.fix-wrong`` (Iteration 2 off), or ``CRG_RULES_ONLY``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter_language_pack import get_parser

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

RULE = 'csharp.import-resolver'
FIX_RULE = 'csharp.fix-wrong'          # Iteration 2
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


# ---- CORE: copied from lib/csharp_callers_ref.py (keep in sync) ----------------------------------------------
# ---------------------------------------------------------------------------------------------------------------
TYPE_DECL = ('class_declaration', 'struct_declaration', 'interface_declaration', 'record_declaration',
             'record_struct_declaration', 'enum_declaration')
INF = 10 ** 6


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _mods(n):
    return {_t(c) for c in n.children if c.type == 'modifier'}


def _name_arity(tn):
    """'Foo<int, X>' -> ('Foo', 2); 'A.B.Foo' -> ('A.B.Foo', 0); strips '?', '[]', 'global::'."""
    s = tn.strip().replace('global::', '')
    s = re.sub(r'\?$', '', s)
    if s.endswith(']'):
        return None, 0
    depth, arity, base = 0, 0, []
    for ch in s:
        if ch == '<':
            depth += 1
            if depth == 1:
                arity = 1
            continue
        if ch == '>':
            depth -= 1
            continue
        if depth == 0:
            base.append(ch)
        elif depth == 1 and ch == ',':
            arity += 1
    b = ''.join(base).strip()
    return (b or None), arity


def _params(plist):
    """(min, max) argument count for a parameter_list."""
    if plist is None:
        return 0, 0
    lo = hi = 0
    for c in plist.children:
        if c.type == 'parameter':
            hi += 1
            if not any(x.type == '=' for x in c.children):
                lo += 1
        elif c.type == 'params':
            hi = INF
    return lo, hi


def _this_param(plist):
    if plist is None:
        return None
    for c in plist.children:
        if c.type == 'parameter':
            if any(x.type == 'modifier' and _t(x) == 'this' for x in c.children):
                return _t(c.child_by_field_name('type'))
            return None
    return None


def _argc(args):
    return sum(1 for c in (args.children if args is not None else []) if c.type == 'argument')


def parse_file(path):
    src = Path(path).read_bytes()
    tree = get_parser('csharp').parse(src)
    out = {'usings': [], 'static_usings': [], 'aliases': {}, 'global_usings': [], 'global_static': [],
           'global_aliases': {}, 'types': [], 'calls': [], 'news': 0}

    def handle_using(n, ns):
        decl_ns = '.'.join(ns)
        is_global = any(c.type == 'global' for c in n.children)
        is_static = any(c.type == 'static' for c in n.children)
        alias = n.child_by_field_name('name')
        names = [c for c in n.named_children if c.type in ('qualified_name', 'identifier', 'generic_name',
                                                           'alias_qualified_name')]
        if alias is not None and any(c.type == '=' for c in n.children):
            tgt = names[-1] if names else None
            (out['global_aliases'] if is_global else out['aliases'])[_t(alias)] = (_t(tgt).replace('global::', ''), decl_ns)
            return
        if not names:
            return
        nm = (_t(names[-1]).replace('global::', ''), decl_ns)
        if is_static:
            (out['global_static'] if is_global else out['static_usings']).append(nm)
        else:
            (out['global_usings'] if is_global else out['usings']).append(nm)

    def method_scope(body_nodes, params_nodes, type_params):
        """name -> declared type text (None = untyped/var/ambiguous) for params, locals, foreach, catch, patterns."""
        scope, local_funcs = {}, set()

        def put(name, typ):
            if name in scope and scope[name] != typ:
                scope[name] = None
            else:
                scope[name] = typ
        for pl in params_nodes:
            for c in (pl.children if pl is not None else []):
                if c.type == 'parameter':
                    nm, ty = c.child_by_field_name('name'), c.child_by_field_name('type')
                    if nm is not None:
                        put(_t(nm), _t(ty) if ty is not None else None)
                elif c.type == 'identifier':      # the name after `params T[]`
                    put(_t(c), None)
        stack = [b for b in body_nodes if b is not None]
        while stack:
            n = stack.pop()
            t = n.type
            if t == 'variable_declaration':
                ty = n.child_by_field_name('type')
                tt = None if ty is None or ty.type == 'implicit_type' else _t(ty)
                for d in n.named_children:
                    if d.type == 'variable_declarator':
                        nm = d.child_by_field_name('name') or (d.named_children[0] if d.named_children else None)
                        if nm is not None:
                            put(_t(nm), tt)
            elif t == 'foreach_statement':
                ty, nm = n.child_by_field_name('type'), n.child_by_field_name('left')
                if nm is None:
                    ids = [c for c in n.children if c.type == 'identifier']
                    nm = ids[1] if len(ids) > 1 else (ids[0] if ids and ty is not None and ty.type == 'implicit_type' else None)
                if nm is not None:
                    put(_t(nm), None if ty is None or ty.type == 'implicit_type' else _t(ty))
            elif t in ('catch_declaration', 'declaration_expression', 'declaration_pattern'):
                ty = n.child_by_field_name('type')
                nm = n.child_by_field_name('name')
                if nm is not None:
                    put(_t(nm), None if ty is None or ty.type == 'implicit_type' else _t(ty))
            elif t in ('lambda_expression', 'anonymous_method_expression'):
                pl = n.child_by_field_name('parameters')
                for c in (pl.named_children if pl is not None and pl.type == 'parameter_list' else []):
                    nm, ty = c.child_by_field_name('name'), c.child_by_field_name('type')
                    if nm is not None:
                        put(_t(nm), _t(ty) if ty is not None else None)
                if pl is not None and pl.type == 'identifier':
                    put(_t(pl), None)
                for c in n.children:
                    if c.type == 'identifier' and pl is None:
                        put(_t(c), None)
            elif t == 'local_function_statement':
                nm = n.child_by_field_name('name')
                if nm is not None:
                    local_funcs.add(_t(nm))
            elif t in ('using_statement', 'fixed_statement'):
                pass
            elif t in TYPE_DECL:
                continue
            stack.extend(n.children)
        return scope, local_funcs

    def walk_calls(node, ctx, scope, local_funcs, tparams):
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in TYPE_DECL:
                continue                                 # nested types walked with their own context
            if n.type in ('object_creation_expression', 'implicit_object_creation_expression'):
                out['news'] += 1
            if n.type == 'invocation_expression':
                fn, args = n.child_by_field_name('function'), n.child_by_field_name('arguments')
                argc, line = _argc(args), n.start_point[0] + 1
                call = None
                if fn is not None:
                    if fn.type in ('identifier', 'generic_name'):
                        nm = _t(fn.child_by_field_name('name') or fn) if fn.type == 'generic_name' else _t(fn)
                        if fn.type == 'generic_name':
                            nm = _t(fn.named_children[0])
                        call = {'kind': 'bare', 'name': nm, 'pline': line}
                    elif fn.type == 'member_access_expression':
                        e, nmn = fn.child_by_field_name('expression'), fn.child_by_field_name('name')
                        if nmn is not None:
                            nm = _t(nmn.named_children[0]) if nmn.type == 'generic_name' else _t(nmn)
                            call = {'kind': 'member', 'name': nm, 'pline': nmn.start_point[0] + 1,
                                    'recv_type': e.type if e is not None else None, 'recv': _t(e)}
                if call is not None:
                    call.update({'line': line, 'argc': argc, 'ctx': ctx, 'scope': scope,
                                 'local_funcs': local_funcs, 'tparams': tparams})
                    out['calls'].append(call)
            stack.extend(n.children)

    def walk(n, ns, enclosing):
        t = n.type
        if t == 'using_directive':
            handle_using(n, ns)
            return
        if t == 'file_scoped_namespace_declaration':
            nm = n.child_by_field_name('name')
            ns[:] = ns + [_t(nm)]
            for c in n.children:
                walk(c, ns, enclosing)
            return
        if t == 'namespace_declaration':
            nm = n.child_by_field_name('name')
            inner = ns + [_t(nm)]
            body = n.child_by_field_name('body')
            for c in (body.children if body is not None else []):
                walk(c, inner, enclosing)
            return
        if t in TYPE_DECL:
            nm = n.child_by_field_name('name')
            tpl = next((c for c in n.children if c.type == 'type_parameter_list'), None)
            tps = [_t(p.child_by_field_name('name') or p) for p in (tpl.named_children if tpl is not None else [])]
            full_ns = '.'.join(ns)
            outer = enclosing[-1] if enclosing else None
            local = (outer['local'] + '.' + _t(nm)) if outer else _t(nm)
            bl = next((c for c in n.children if c.type == 'base_list'), None)
            bases = [_t(c) for c in (bl.named_children if bl is not None else [])
                     if c.type not in ('argument_list', 'primary_constructor_base_type')]
            for c in (bl.named_children if bl is not None else []):
                if c.type == 'primary_constructor_base_type':
                    bases.append(_t(c.named_children[0]) if c.named_children else _t(c))
            ty = {'ns': full_ns, 'local': local, 'name': _t(nm), 'arity': len(tps), 'kind': t,
                  'mods': _mods(n), 'line': n.start_point[0] + 1, 'bases': bases, 'methods': defaultdict(list),
                  'members': {}, 'tparams': tps, 'outer': outer, 'nested': {}}
            if outer is not None:
                outer['nested'].setdefault(_t(nm), []).append(ty)
            out['types'].append(ty)
            ctx = {'ns': list(ns), 'types': enclosing + [ty]}
            all_tps = set(tps) | {p for e in enclosing for p in e['tparams']}
            if t == 'record_declaration':
                pl = next((c for c in n.children if c.type == 'parameter_list'), None)
                for c in (pl.children if pl is not None else []):
                    if c.type == 'parameter' and c.child_by_field_name('name') is not None:
                        ty['members'][_t(c.child_by_field_name('name'))] = _t(c.child_by_field_name('type'))
            body = n.child_by_field_name('body')
            for m in (body.named_children if body is not None else []):
                mt = m.type
                if mt in TYPE_DECL:
                    walk(m, list(ns), enclosing + [ty])
                elif mt == 'method_declaration':
                    mn = m.child_by_field_name('name')
                    pl = m.child_by_field_name('parameters')
                    mtpl = next((c for c in m.children if c.type == 'type_parameter_list'), None)
                    mtps = {_t(p.child_by_field_name('name') or p) for p in (mtpl.named_children if mtpl is not None else [])}
                    lo, hi = _params(pl)
                    mods = _mods(m)
                    if t == 'interface_declaration' and m.child_by_field_name('body') is None and \
                            not any(c.type == 'arrow_expression_clause' for c in m.children):
                        mods = mods | {'abstract'}
                    ty['methods'][_t(mn)].append({'file': path, 'line': m.start_point[0] + 1, 'lo': lo, 'hi': hi,
                                                  'mods': mods, 'this': _this_param(pl), 'name': _t(mn),
                                                  'mtps': mtps})
                    body_nodes = [m.child_by_field_name('body')] + [c for c in m.children if c.type == 'arrow_expression_clause']
                    scope, lf = method_scope(body_nodes, [pl], all_tps | mtps)
                    for b in body_nodes:
                        if b is not None:
                            walk_calls(b, ctx, scope, lf, all_tps | mtps)
                elif mt in ('constructor_declaration', 'destructor_declaration', 'operator_declaration',
                            'conversion_operator_declaration', 'accessor_declaration'):
                    pl = m.child_by_field_name('parameters')
                    body_nodes = [m.child_by_field_name('body')] + [c for c in m.children if c.type in ('arrow_expression_clause', 'constructor_initializer')]
                    scope, lf = method_scope(body_nodes, [pl], all_tps)
                    for b in body_nodes:
                        if b is not None:
                            walk_calls(b, ctx, scope, lf, all_tps)
                elif mt in ('field_declaration', 'event_field_declaration'):
                    vd = next((c for c in m.named_children if c.type == 'variable_declaration'), None)
                    if vd is not None:
                        vt = vd.child_by_field_name('type')
                        for d in vd.named_children:
                            if d.type == 'variable_declarator':
                                dn = d.child_by_field_name('name') or (d.named_children[0] if d.named_children else None)
                                if dn is not None:
                                    ty['members'][_t(dn)] = None if vt is None or vt.type == 'implicit_type' else _t(vt)
                        scope, lf = method_scope([vd], [], all_tps)
                        walk_calls(vd, ctx, {}, set(), all_tps)
                elif mt in ('property_declaration', 'indexer_declaration', 'event_declaration'):
                    pn, pt = m.child_by_field_name('name'), m.child_by_field_name('type')
                    if pn is not None and mt == 'property_declaration':
                        ty['members'][_t(pn)] = _t(pt) if pt is not None else None
                    for acc in [c for c in m.children if c.type in ('accessor_list', 'arrow_expression_clause')] + \
                               [c for c in m.children if c.type == 'equals_value_clause']:
                        nodes = acc.named_children if acc.type == 'accessor_list' else [acc]
                        for a in nodes:
                            pl = m.child_by_field_name('parameters')
                            scope, lf = method_scope([a], [pl] if pl is not None else [], all_tps)
                            walk_calls(a, ctx, scope, lf, all_tps)
            return
        if t == 'global_statement':
            walk_calls(n, {'ns': list(ns), 'types': []}, {}, set(), set())
            return
        for c in n.children:
            walk(c, ns, enclosing)

    walk(tree.root_node, [], [])
    for ty in out['types']:
        ty['methods'] = dict(ty['methods'])
        ty['outer_key'] = None
    return out


def _project_of(f, projects):
    d = os.path.dirname(f)
    while True:
        if d in projects:
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return None
        d = nd


_USING_RE = re.compile(r'<Using\s+Include="([^"]+)"([^>]*)/?>')


class Index:
    """Repo-wide type index and call classifier (see module docstring)."""

    def __init__(self, files, root):
        self.root = str(root).rstrip('/')
        self.P = {}
        for f in files:
            try:
                self.P[f] = parse_file(f)
            except (OSError, ValueError):
                pass
        projs = set()
        for dp, dn, fn in os.walk(self.root):
            dn[:] = [d for d in dn if d not in ('.git', 'bin', 'obj', 'node_modules')]
            if any(x.endswith('.csproj') for x in fn):
                projs.add(dp)
        self.projects = projs
        self.proj = {f: (_project_of(f, projs) or self.root) for f in self.P}
        # project-level global usings: from `global using` in any file of the project and <Using Include> items
        self.g_usings, self.g_static, self.g_alias = defaultdict(set), defaultdict(set), defaultdict(dict)
        for f, p in self.P.items():
            pr = self.proj[f]
            self.g_usings[pr].update(p['global_usings'])
            self.g_static[pr].update(p['global_static'])
            self.g_alias[pr].update(p['global_aliases'])
        for pr in projs | {self.root}:
            items = []
            d = pr
            while d.startswith(self.root):
                for x in os.listdir(d):
                    if x == 'Directory.Build.props' or (d == pr and x.endswith('.csproj')):
                        try:
                            items += _USING_RE.findall(Path(d, x).read_text(errors='replace'))
                        except OSError:
                            pass
                if d == self.root:
                    break
                d = os.path.dirname(d)
            for inc, rest in items:
                if 'Alias=' in rest:
                    a = re.search(r'Alias="([^"]+)"', rest)
                    if a:
                        self.g_alias[pr][a.group(1)] = (inc, '')
                elif 'Static="true"' in rest:
                    self.g_static[pr].add((inc, ''))
                else:
                    self.g_usings[pr].add((inc, ''))
        # types: (full name, arity) -> project -> merged type
        self.types = defaultdict(dict)
        self.namespaces = set()
        for f, p in self.P.items():
            pr = self.proj[f]
            for ty in p['types']:
                full = (ty['ns'] + '.' if ty['ns'] else '') + ty['local']
                key = (full, ty['arity'])
                ty['file'] = f
                g = self.types[key].get(pr)
                if g is None:
                    g = {'key': key, 'proj': pr, 'decls': [], 'methods': defaultdict(list), 'members': {},
                         'kind': ty['kind'], 'mods': set(), 'files': set()}
                    self.types[key][pr] = g
                g['decls'].append(ty)
                g['mods'] |= ty['mods']
                g['files'].add(f)
                for nm, ms in ty['methods'].items():
                    g['methods'][nm].extend(ms)
                g['members'].update(ty['members'])
                ty['group'] = g
                parts = ty['ns'].split('.') if ty['ns'] else []
                for i in range(1, len(parts) + 1):
                    self.namespaces.add('.'.join(parts[:i]))
        # a non-partial type declared twice in one project is a conflict (e.g. #if variants): keep but mark
        for key, byp in self.types.items():
            for g in byp.values():
                g['conflict'] = len(g['decls']) > 1 and not all('partial' in d['mods'] for d in g['decls'])
        # `using X;` written inside `namespace A.B { }` means A.B.X, then A.X, then X (C# resolves it relative to the
        # enclosing namespace). Turn every recorded (name, namespace written in) into the absolute name.
        self.type_names = {k[0] for k in self.types}
        for f, p in self.P.items():
            p['usings'] = [self._abs(u, d, self.namespaces) for u, d in p['usings']]
            p['static_usings'] = [self._abs(u, d, self.type_names) for u, d in p['static_usings']]
            p['aliases'] = {a: self._abs(u, d, self.namespaces | self.type_names) for a, (u, d) in p['aliases'].items()}
        for pr in list(self.g_usings):
            self.g_usings[pr] = {self._abs(u, d, self.namespaces) for u, d in self.g_usings[pr]}
        for pr in list(self.g_static):
            self.g_static[pr] = {self._abs(u, d, self.type_names) for u, d in self.g_static[pr]}
        for pr in list(self.g_alias):
            self.g_alias[pr] = {a: self._abs(u, d, self.namespaces | self.type_names) for a, (u, d) in self.g_alias[pr].items()}
        self.short = defaultdict(list)            # simple name -> keys (for extension lookup etc.)
        for key in self.types:
            self.short[key[0].rsplit('.', 1)[-1]].append(key)
        self._base_cache, self._anc_cache, self._bases_done = {}, {}, False
        self._resolve_bases()
        self._bases_done = True
        self._ext = defaultdict(list)            # extension method name -> [(ns, group, method)]
        for key, byp in self.types.items():
            for g in byp.values():
                if 'static' not in g['mods']:
                    continue
                ns = g['decls'][0]['ns']
                for nm, ms in g['methods'].items():
                    for m in ms:
                        if m['this']:
                            self._ext[nm].append((ns, g, m))

    @staticmethod
    def _abs(name, decl_ns, known):
        base, _ = _name_arity(name)
        parts = decl_ns.split('.') if decl_ns else []
        for i in range(len(parts), 0, -1):
            cand = '.'.join(parts[:i]) + '.' + (base or name)
            if cand in known:
                return '.'.join(parts[:i]) + '.' + name
        return name

    # -- type resolution -----------------------------------------------------------------------------------------
    def _pick(self, key, f):
        byp = self.types.get(key)
        if not byp:
            return None
        pr = self.proj.get(f)
        if pr in byp:
            return byp[pr]
        if len(byp) == 1:
            return next(iter(byp.values()))
        best, score = [], -1
        for p, g in byp.items():
            s = len(os.path.commonpath([f, p]))
            if s > score:
                best, score = [g], s
            elif s == score:
                best.append(g)
        return best[0] if len(best) == 1 else None

    def resolve_type(self, text, f, ctx, tparams=()):
        """Type text in the context (file f, namespaces, enclosing types) -> merged type group, or None."""
        if not text:
            return None
        base, arity = _name_arity(text)
        if base is None or base in tparams:
            return None
        p = self.P[f]
        pr = self.proj[f]
        if '.' in base:
            first, rest = base.split('.', 1)
            aliases = {**self.g_alias[pr], **p['aliases']}
            if first in aliases:
                ab, _ = _name_arity(aliases[first])
                if ab:
                    g = self._pick((ab + '.' + rest, arity), f)
                    if g:
                        return g
            g = self._pick((base, arity), f)
            if g:
                return g
            ns = ctx['ns']
            joined = '.'.join(ns)
            parts = joined.split('.') if joined else []
            for i in range(len(parts), 0, -1):
                g = self._pick(('.'.join(parts[:i]) + '.' + base, arity), f)
                if g:
                    return g
            # Outer.Inner where Outer resolves as a simple name
            og = self.resolve_type(first, f, ctx, tparams)
            if og is not None:
                return self._pick((og['key'][0] + '.' + rest, arity), f)
            return None
        # nested types of enclosing types (and of their repo base types)
        for ty in reversed(ctx['types']):
            g = ty.get('group')
            for grp in (([g] + self.ancestors(g)[0]) if g else []):
                k = (grp['key'][0] + '.' + base, arity)
                if k in self.types:
                    r = self._pick(k, f)
                    if r:
                        return r
        joined = '.'.join(ctx['ns'])
        parts = joined.split('.') if joined else []
        for i in range(len(parts), -1, -1):
            k = (('.'.join(parts[:i]) + '.' if i else '') + base, arity)
            if k in self.types:
                r = self._pick(k, f)
                if r:
                    return r
        aliases = {**self.g_alias[pr], **p['aliases']}
        if base in aliases and arity == 0:
            ab, aa = _name_arity(aliases[base])
            return self._pick((ab, aa), f) if ab else None
        hits = []
        for u in set(p['usings']) | self.g_usings[pr]:
            k = (u + '.' + base, arity)
            if k in self.types:
                r = self._pick(k, f)
                if r:
                    hits.append(r)
        if len({id(h) for h in hits}) == 1:
            return hits[0]
        return None

    def _resolve_bases(self):
        for key, byp in self.types.items():
            for g in byp.values():
                cls, ifaces, external = None, [], False
                for d in g['decls']:
                    ctx = {'ns': d['ns'].split('.') if d['ns'] else [], 'types': self._chain(d)}
                    for i, b in enumerate(d['bases']):
                        r = self.resolve_type(b, d['file'], ctx, set(d['tparams']))
                        if r is None:
                            bn, _ = _name_arity(b)
                            if bn and re.match(r'^I[A-Z]', bn.rsplit('.', 1)[-1]):
                                continue                    # external interface: no members for class lookup
                            if i == 0 and g['kind'] in ('class_declaration', 'record_declaration'):
                                external = True
                            continue
                        if r['kind'] == 'interface_declaration':
                            ifaces.append(r)
                        elif r is not g:
                            cls = r
                g['base'], g['ifaces'], g['external_base'] = cls, ifaces, external
        self.subtypes = defaultdict(list)
        for byp in self.types.values():
            for g in byp.values():
                for b in ([g['base']] if g['base'] else []) + g['ifaces']:
                    self.subtypes[id(b)].append(g)

    def _chain(self, d):
        out, x = [], d
        while x is not None:
            out.insert(0, x)
            x = x['outer']
        return out

    def ancestors(self, g):
        """(repo ancestors: base classes nearest first, then interfaces; chain_complete)."""
        if g is None:
            return [], False
        b, i, complete = self.split_ancestors(g)
        return b + i, complete

    def split_ancestors(self, g):
        if id(g) in self._anc_cache:
            return self._anc_cache[id(g)]
        bases, seen, complete = [], {id(g)}, True
        x = g
        while x is not None:
            if x.get('external_base'):
                complete = False
            nxt = x.get('base')
            if nxt is None or id(nxt) in seen:
                break
            seen.add(id(nxt))
            bases.append(nxt)
            x = nxt
        ifaces, todo = [], [g] + list(bases)
        while todo:
            y = todo.pop(0)
            for i in y.get('ifaces', []) or []:
                if id(i) not in seen:
                    seen.add(id(i))
                    ifaces.append(i)
                    todo.append(i)
        r = (bases, ifaces, complete)
        if self._bases_done:
            self._anc_cache[id(g)] = r
        return r

    def all_subtypes(self, g):
        out, todo, seen = [], [g], {id(g)}
        while todo:
            y = todo.pop()
            for s in self.subtypes.get(id(y), []):
                if id(s) not in seen:
                    seen.add(id(s))
                    out.append(s)
                    todo.append(s)
        return out

    # -- member lookup -------------------------------------------------------------------------------------------
    @staticmethod
    def _fits(m, n):
        return m['lo'] <= n <= m['hi']

    def lookup(self, g, name, n, static_only=False):
        """The method `name` that a call with n arguments on type g reaches (most-derived declaration first; an
        override or `new` member hides the same-shaped one above it). Classes: own type and base-class chain, and
        interfaces only when the class chain has none. Returns (status, method, owner), status in
        found / overload / none / none-name / incomplete."""
        bases, ifaces, complete = self.split_ancestors(g)
        any_name = False

        def collect(groups):
            nonlocal any_name
            cands, shapes = [], set()
            for grp in groups:
                here = set()
                for m in grp['methods'].get(name, []):
                    any_name = True
                    shape = (m['lo'], m['hi'])
                    if shape in shapes:
                        continue                     # hidden by / overridden in a more-derived type
                    here.add(shape)
                    if self._fits(m, n) and (not static_only or 'static' in m['mods']):
                        cands.append((m, grp))
                shapes |= here
            return cands
        if g['kind'] == 'interface_declaration':
            cands = collect([g] + ifaces)
        else:
            cands = collect([g] + bases) or collect(ifaces)
        if len(cands) == 1:
            return 'found', cands[0][0], cands[0][1]
        if len(cands) > 1:
            return 'overload', None, None
        if not complete:
            return 'incomplete', None, None
        return ('none-name' if not any_name else 'none'), None, None

    def overridden_below(self, g, name, n):
        for s in self.all_subtypes(g):
            for m in s['methods'].get(name, []):
                if 'override' in m['mods'] and self._fits(m, n):
                    return True
        return False

    def implementations(self, iface):
        return [s for s in self.all_subtypes(iface) if s['kind'] != 'interface_declaration'
                and 'abstract' not in s['mods']]

    def _visible_namespaces(self, f, ctx):
        p, pr = self.P[f], self.proj[f]
        joined = '.'.join(ctx['ns'])
        parts = joined.split('.') if joined else []
        return {'.'.join(parts[:i]) for i in range(len(parts) + 1)} | set(p['usings']) | self.g_usings[pr]

    def extension(self, f, ctx, name, n, recv_group):
        """The single extension method `name` in scope applicable to recv_group as `this`, or None/'many'."""
        names = {recv_group['key'][0].rsplit('.', 1)[-1]} | {a['key'][0].rsplit('.', 1)[-1] for a in self.ancestors(recv_group)[0]}
        vis = self._visible_namespaces(f, ctx)
        hits = []
        for ns, g, m in self._ext.get(name, []):
            if ns not in vis or not self._fits(m, n + 1):
                continue
            tb, _ = _name_arity(m['this'])
            tb = (tb or '').rsplit('.', 1)[-1]
            if tb in names or tb in m['mtps']:
                hits.append((g, m))
        return hits

    def any_extension(self, name, n):
        return any(self._fits(m, n + 1) for _, _, m in self._ext.get(name, []))

    # -- classification -----------------------------------------------------------------------------------------
    def classify(self, f, c):
        """-> dict(cls=..., targets=[(file, line)], via=...)"""
        ctx, scope, tps = c['ctx'], c['scope'], c['tparams']
        name, n = c['name'], c['argc']
        encl = ctx['types']
        enc_group = encl[-1]['group'] if encl else None

        def member_type(x):
            """Declared type text of x as a local/param (scope) or as a field/property of the enclosing types."""
            if x in scope:
                return ('local', scope[x])
            for ty in reversed(encl):
                g = ty['group']
                for grp in [g] + self.ancestors(g)[0]:
                    if x in grp['members']:
                        return ('member', grp['members'][x])
            return None

        def result(m, owner, via, recv_group=None):
            if m is None:
                return {'cls': 'unknown'}
            targets = [(m['file'], m['line'])]
            if m['file'] == f and via not in ('interface, one implementation',):
                return {'cls': 'same-file'}
            return {'cls': 'certain', 'targets': targets, 'via': via}

        if c['kind'] == 'bare':
            if name in c['local_funcs'] or (name in scope):
                return {'cls': 'unknown'}
            for ty in reversed(encl):
                g = ty['group']
                if not any(name in grp['methods'] for grp in [g] + self.ancestors(g)[0]):
                    if not self.ancestors(g)[1] and ty is encl[-1]:
                        return {'cls': 'unknown'}          # could be inherited from a non-repo base
                    continue
                st, m, owner = self.lookup(g, name, n)
                if st != 'found':
                    return {'cls': st if st == 'overload' else 'unknown'}
                if 'abstract' in m['mods'] or self.overridden_below(g, name, n):
                    return {'cls': 'virtual'}
                via = 'partial class' if owner is g else 'inherited'
                return result(m, owner, via)
            # using static
            p, pr = self.P[f], self.proj[f]
            hits = []
            for u in set(p['static_usings']) | self.g_static[pr]:
                g = self.resolve_type(u, f, {'ns': [], 'types': []})
                if g is None:
                    continue
                st, m, owner = self.lookup(g, name, n, static_only=True)
                if st == 'found':
                    hits.append((m, owner))
                elif st == 'overload':
                    return {'cls': 'overload'}
            if len(hits) == 1:
                return result(hits[0][0], hits[0][1], 'using static')
            return {'cls': 'unknown'}

        rt, recv = c['recv_type'], c['recv']
        if rt == 'base':
            if enc_group is None or enc_group.get('base') is None:
                return {'cls': 'unknown'}
            st, m, owner = self.lookup(enc_group['base'], name, n)
            return result(m, owner, 'base call') if st == 'found' else {'cls': st if st == 'overload' else 'unknown'}
        if rt == 'this':
            c2 = dict(c, kind='bare')
            if name in scope or name in c['local_funcs']:
                c2 = dict(c2, scope={k: v for k, v in scope.items() if k != name}, local_funcs=set())
            r = self.classify(f, c2)
            return r if r['cls'] != 'certain' or r['via'] != 'using static' else {'cls': 'unknown'}
        if rt in ('identifier', 'generic_name', 'qualified_name', 'member_access_expression', 'alias_qualified_name'):
            first = recv.split('.', 1)[0].split('<', 1)[0]
            mt = member_type(first) if rt == 'identifier' else (member_type(first) if rt == 'member_access_expression' else None)
            if rt == 'identifier' and mt is not None:
                kind, typ = mt
                if typ is None:
                    return {'cls': 'unknown'}
                g = self.resolve_type(typ, f, ctx, tps)
                if g is None:
                    tb, _ = _name_arity(typ)
                    if tb and tb not in tps and not self.any_extension(name, n):
                        return {'cls': 'external'}
                    return {'cls': 'unknown'}
                if g['kind'] == 'interface_declaration':
                    st, m, owner = self.lookup(g, name, n)
                    if st != 'found':
                        return {'cls': 'unknown' if st != 'overload' else 'overload'}
                    impls = self.implementations(g)
                    if len(impls) != 1:
                        return {'cls': 'virtual'}
                    st2, m2, owner2 = self.lookup(impls[0], name, n)
                    if st2 != 'found' or 'abstract' in m2['mods'] or self.overridden_below(impls[0], name, n):
                        return {'cls': 'virtual'}
                    targets = [(m2['file'], m2['line']), (m['file'], m['line'])]
                    if all(t[0] == f for t in targets):
                        return {'cls': 'same-file'}
                    return {'cls': 'certain', 'targets': targets, 'via': 'interface, one implementation'}
                st, m, owner = self.lookup(g, name, n)
                if st == 'found':
                    if 'static' in m['mods']:
                        return {'cls': 'unknown'}
                    if 'abstract' in m['mods'] or self.overridden_below(g, name, n):
                        return {'cls': 'virtual'}
                    return result(m, owner, 'typed receiver')
                if st == 'overload':
                    return {'cls': 'overload'}
                if st in ('none', 'none-name') and self.ancestors(g)[1]:
                    hits = self.extension(f, ctx, name, n, g)
                    if len(hits) == 1:
                        return result(hits[0][1], hits[0][0], 'extension method')
                return {'cls': 'unknown'}
            if rt == 'member_access_expression' and mt is not None:
                return {'cls': 'unknown'}                # property chain on a local / member
            if rt == 'identifier' and first in c['local_funcs']:
                return {'cls': 'unknown'}
            g = self.resolve_type(recv, f, ctx, tps)
            if g is not None:
                st, m, owner = self.lookup(g, name, n)
                if st == 'found':
                    return result(m, owner, 'static call T.M()')
                return {'cls': st if st == 'overload' else 'unknown'}
            if rt == 'identifier' and recv[:1].isupper() and enc_group is not None:
                chain_ok = all(self.ancestors(ty['group'])[1] for ty in encl)
                if chain_ok and recv not in self.namespaces and not any(
                        (ns + '.' + recv) in self.namespaces for ns in self._visible_namespaces(f, ctx)):
                    return {'cls': 'external'}
            return {'cls': 'unknown'}
        return {'cls': 'unknown'}

    def certain_calls(self):
        out, counts = [], defaultdict(int)
        for f, p in self.P.items():
            for c in p['calls']:
                r = self.classify(f, c)
                counts[r['cls']] += 1
                rec = {'file': f, 'line': c['line'], 'pline': c['pline'], 'name': c['name'], 'cls': r['cls']}
                if r['cls'] == 'certain':
                    rec.update(targets_at=r['targets'], via=r['via'],
                               target_file=r['targets'][0][0], target_line=r['targets'][0][1])
                out.append(rec)
        return out, counts
# ---- END CORE ---------------------------------------------------------------------------------------------------


def _short(t):
    tail = t.rsplit('::', 1)[-1].split(':S', 1)[0]
    tail = re.sub(r'<.*$', '', tail)
    return tail.rsplit('.', 1)[-1]


def _base(qn):
    return re.sub(r':S[0-9a-f]+$', '', qn)


def _repo_root(files):
    d = os.path.commonpath(files)
    if os.path.isfile(d):
        d = os.path.dirname(d)
    x = d
    while True:
        if os.path.isdir(os.path.join(x, '.git')) or os.path.isdir(os.path.join(x, '.code-review-graph')):
            return x
        nx = os.path.dirname(x)
        if nx == x:
            return d
        x = nx


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'files': 0, 'certain_calls': 0, 'calls_resolved': 0, 'already_bound': 0,
             'no_crg_edge': 0, 'no_target_node': 0, 'other_binding_kept': 0, 'fix_wrong_corrected': 0,
             'external_unbound': 0}
    if not stats['enabled']:
        return stats
    from .rules import switch_on
    fix_wrong = switch_on(FIX_RULE)
    stats['fix_wrong_enabled'] = fix_wrong
    files = [f for f in store.get_all_files() if f.endswith('.cs')]
    if not files:
        return stats
    root = _repo_root(files)
    # CRG's own C# pass (csharp_resolver, run by scoped_resolver) re-derives every C# CALLS edge from
    # `csharp_raw_target` and runs AFTER this hook in the build. Run it here first so the bindings judged below are
    # CRG's own, then hide `csharp_raw_target` on the edges this pass rewrites (kept as `csharp_raw_target_fork`)
    # so CRG's later run leaves them alone. The pass is idempotent: its later run gives the same result elsewhere.
    try:
        from .csharp_resolver import resolve_csharp_calls
        stats['crg_csharp_pre_pass'] = resolve_csharp_calls(store, Path(root))
    except Exception as exc:  # noqa: BLE001 - best effort, like every post-pass
        logger.warning('C# resolver: CRG pre-pass failed: %s', exc)
    idx = Index(files, root)
    stats['files'] = len(idx.P)
    calls, _ = idx.certain_calls()
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    fileset = set(idx.P)
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
    # every call per (file, line, name): a binding is judged only when all calls of that name on the line are known
    on_line = defaultdict(list)
    for c in calls:
        for ln in {c['line'], c['pline']}:
            on_line[(c['file'], ln, c['name'])].append(c)

    updates, done = [], set()
    for c in calls:
        f, name = c['file'], c['name']
        lines = dict.fromkeys((c['line'], c['pline']))
        if c['cls'] == 'external':
            if not fix_wrong:
                continue
            if not all(x['cls'] == 'external' for ln in lines for x in on_line[(f, ln, name)]):
                continue
            for ln in lines:
                for eid, tq, extra in edges_at.get((f, ln), []):
                    if eid in done or _short(tq) != name or tq not in node_file:
                        continue
                    dd = json.loads(extra or '{}') or {}
                    for k in DROP:
                        dd.pop(k, None)
                    dd.update({'unresolved_targets': [], 'rule': FIX_RULE, 'csharp_via': 'external (no repo target)',
                               'csharp_replaced': tq})
                    if 'csharp_raw_target' in dd:
                        dd['csharp_raw_target_fork'] = dd.pop('csharp_raw_target')
                    updates.append((name, json.dumps(dd), eid))
                    done.add(eid)
                    stats['external_unbound'] += 1
            continue
        if c['cls'] != 'certain':
            continue
        stats['certain_calls'] += 1
        accepted = []
        for t in c['targets_at']:
            nodes = node_at.get(t, [])
            if len(nodes) > 1:
                nodes = [q for q in nodes if _short(q) == name] or nodes
            if len(nodes) == 1:
                accepted.append(nodes[0])
        if not accepted:
            stats['no_target_node'] += 1
            continue
        target = accepted[0]
        cands = [e for ln in lines for e in edges_at.get((f, ln), []) if _short(e[1]) == name and e[0] not in done]
        if not cands:
            stats['no_crg_edge'] += 1
            continue
        if any(e[1] in accepted and 'collision_endpoint' not in (e[2] or '') for e in cands):
            stats['already_bound'] += 1
            continue
        for eid, tq, extra in cands:
            rule = RULE
            if tq in node_file and _base(tq) not in {_base(a) for a in accepted}:
                same_name_calls = [x for ln in lines for x in on_line[(f, ln, name)]]
                if not fix_wrong or not all(x['cls'] in ('certain', 'external') for x in same_name_calls):
                    stats['other_binding_kept'] += 1
                    continue
                rule = FIX_RULE                     # Iteration 2: the C# rules prove CRG's binding wrong
            dd = json.loads(extra or '{}') or {}
            for k in DROP:
                dd.pop(k, None)
            if 'csharp_raw_target' in dd:
                dd['csharp_raw_target_fork'] = dd.pop('csharp_raw_target')
            extra_new = {**dd, 'rule': rule, 'csharp_via': c['via']}
            if rule == FIX_RULE:
                extra_new.update({'collision_evidence': FIX_RULE, 'csharp_replaced': tq})
                stats['fix_wrong_corrected'] += 1
            updates.append((target, json.dumps(extra_new), eid))
            done.add(eid)
            break

    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates) - stats['external_unbound']
    logger.info('C# import resolution: %s', stats)
    return stats
