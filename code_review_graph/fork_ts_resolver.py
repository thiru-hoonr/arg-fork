"""Post-build pass that binds TypeScript calls whose target is fixed by import/export syntax (our fork).

Stock CRG leaves these cross-file TS calls unbound or wrongly bound (notebooks/11-typescript):

- ``import * as core from "../core/index.js"; core.config()`` where the index does ``export * from "./core.js"``:
  CRG does not follow ``export *`` and leaves ``config`` UNRESOLVED (most of zod's misses)
- ``import {union} from "./schemas.js"; union()`` when several files define ``union``: AMBIGUOUS
- ``Logger.log()`` on an imported class that has both a static and an instance ``log``: AMBIGUOUS collision
- ``createWSClient()`` where the parser claims a same-file target that does not exist
- ``core._undefined()`` bound by CRG to a same-file ``_undefined``: the receiver is an imported namespace, so a
  same-file function cannot be meant

What this pass does, for .ts/.tsx/.mts/.cts files (not .d.ts), on the stored graph:

1. Parse each file with tree-sitter (``typescript``; ``tsx`` for .tsx): imports (named, default, namespace,
   ``import x = require()``), exports (declarations, ``export default``, ``export {a as b}``, ``export =``),
   re-exports (``export {a} from``, ``export * as ns from``, ``export * from``) and call sites. Type-only
   imports/exports are ignored.
2. A relative spec resolves to one repo file: ``./x``, ``.ts``, ``.tsx``, ``.mts``, ``.cts``, ``/index.ts(x)``,
   and ``./x.js`` naming ``./x.ts`` (ESM-style TS). Package imports and tsconfig paths are never touched.
3. A call is bound only when its target is certain from syntax: ``f()`` for an imported ``f`` (or default),
   ``NS.f()`` for a namespace import, ``C.m()`` for an imported class (static ``m`` wins over an instance ``m``)
   or exported object literal. ``export *`` is followed only when exactly one star target exports the name.
4. The CRG CALLS edge on that line (or on the method name's line, for a call over several lines) with that call
   name is rewritten to the CRG node (Function/Class/Test) starting at the definition's line, only when its
   target is not a real node, is a same-name duplicate of ours (``C.m`` vs ``C.m:S<hash>``), or (for a member
   call on an imported receiver) is a node in the caller's own file. Its ``claimed_endpoint``,
   ``ambiguous_*``, ``unresolved_targets`` and ``collision_endpoint`` keys are dropped, so CRG's later
   collision pass does not turn it back into AMBIGUOUS. Nothing is invented: a call with no CRG edge stays so.

5. Non-relative specs (round 2): workspace packages by unique ``package.json`` name (``exports``/``main``/subpath)
   and tsconfig ``paths``/``baseUrl`` of the nearest tsconfig (class ``Workspace``); a spec that does not name
   exactly one repo file is left alone, as before.
6. Iteration 2, rule ``ts.fix-wrong`` (switch via ``rules.switch_on``): when the syntax-certain target is known
   and CRG bound that call's edge to a DIFFERENT real node, the edge is rewritten to the certain target, tagged
   ``rule='ts.fix-wrong'``, ``collision_evidence='ts.fix-wrong'`` and ``replaced_target`` (CRG's node). Only when
   every call with that name on that line is certain and agrees on the target (a local ``f()`` next to an
   imported ``ns.f()`` blocks it).

Every rewritten edge gets ``extra.rule = 'ts.import-resolver'`` and ``extra.ts_import`` (the import spec).
Switch: rule name ``ts.import-resolver``; off with ``CRG_RULES=off`` or ``CRG_RULES_OFF=ts.import-resolver``;
with ``CRG_RULES_ONLY`` set it runs only when named there. Called from the shared hook in incremental.py.
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

RULE = 'ts.import-resolver'
FIX_RULE = 'ts.fix-wrong'          # Iteration 2


def fix_enabled() -> bool:
    try:
        from .rules import switch_on
    except ImportError:            # older fork without the shared helper
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


TS_EXT = ('.ts', '.tsx', '.mts', '.cts')
FUNC = ('function_expression', 'arrow_function', 'function', 'generator_function', 'class')
CLASS_DECL = ('class_declaration', 'abstract_class_declaration')
FUNC_DECL = ('function_declaration', 'generator_function_declaration')


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _str(n):
    if n is None or n.type != 'string':
        return None
    return _t(n)[1:-1]


def _is_type_only(n):
    return any(c.type == 'type' for c in n.children)


def is_ts_source(path):
    return path.endswith(TS_EXT) and not path.endswith('.d.ts')


def parse_file(path, parsers):
    with open(path, 'rb') as fh:
        src = fh.read()
    tree = parsers['tsx' if path.endswith('.tsx') else 'ts'].parse(src)
    defs = {}             # 'f' / 'C' / 'C.m' / 'obj.m' -> line
    imports = {}          # local name -> (spec, member | None (namespace) | 'default' | '=')
    exports = {}          # exported name -> local key (def or import name)
    reexports = {}        # exported name -> (spec, member | None for `* as ns`)
    stars = []            # specs of `export * from`
    calls = []

    def add_class(n):
        name = n.child_by_field_name('name')
        if name is None:
            return None
        cn = _t(name)
        defs[cn] = n.start_point[0] + 1
        body = n.child_by_field_name('body')
        for m in (body.named_children if body is not None else []):
            if m.type in ('method_definition', 'abstract_method_signature'):
                mk = f'{cn}.{_t(m.child_by_field_name("name"))}'
                if any(c.type == 'static' for c in m.children) or mk not in defs:
                    defs[mk] = m.start_point[0] + 1
            elif m.type == 'public_field_definition':
                v = m.child_by_field_name('value')
                if v is not None and v.type in FUNC:
                    mk = f'{cn}.{_t(m.child_by_field_name("name"))}'
                    if any(c.type == 'static' for c in m.children) or mk not in defs:
                        defs[mk] = m.start_point[0] + 1
        return cn

    def add_var(d):
        name, val = d.child_by_field_name('name'), d.child_by_field_name('value')
        if name is None or name.type != 'identifier':
            return None
        nm = _t(name)
        while val is not None and val.type in ('as_expression', 'satisfies_expression', 'parenthesized_expression',
                                               'non_null_expression'):
            val = val.named_children[0] if val.named_children else None
        if val is None:
            return nm
        if val.type in FUNC:
            defs[nm] = d.start_point[0] + 1
        elif val.type == 'object':
            for p in val.named_children:
                v = p.child_by_field_name('value') if p.type == 'pair' else None
                if v is not None and v.type in FUNC:
                    defs[f"{nm}.{_t(p.child_by_field_name('key'))}"] = p.start_point[0] + 1
                elif p.type == 'method_definition':
                    defs[f"{nm}.{_t(p.child_by_field_name('name'))}"] = p.start_point[0] + 1
        return nm

    def walk(n):
        t = n.type
        line = n.start_point[0] + 1
        if t in FUNC_DECL:
            name = n.child_by_field_name('name')
            if name is not None:
                defs.setdefault(_t(name), line)      # first of several overload signatures/impl
        elif t in CLASS_DECL:
            add_class(n)
        elif t == 'variable_declarator':
            add_var(n)
        elif t == 'import_statement' and not _is_type_only(n):
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
                                if s.type != 'import_specifier' or _is_type_only(s):
                                    continue
                                nm, al = s.child_by_field_name('name'), s.child_by_field_name('alias')
                                imports[_t(al or nm)] = (spec, _t(nm))
                elif c.type == 'import_require_clause':
                    ids = [x for x in c.named_children if x.type == 'identifier']
                    s = _str(next((x for x in c.named_children if x.type == 'string'), None))
                    if ids and s:
                        imports[_t(ids[0])] = (s, '=')
        elif t == 'export_statement' and not _is_type_only(n):
            src_spec = _str(n.child_by_field_name('source'))
            decl = n.child_by_field_name('declaration')
            is_default = any(c.type == 'default' for c in n.children)
            if src_spec:
                clause = next((c for c in n.named_children if c.type == 'export_clause'), None)
                ns = next((c for c in n.named_children if c.type == 'namespace_export'), None)
                if clause is not None:
                    for s in clause.named_children:
                        if s.type != 'export_specifier' or _is_type_only(s):
                            continue
                        nm, al = s.child_by_field_name('name'), s.child_by_field_name('alias')
                        reexports[_t(al or nm)] = (src_spec, _t(nm))
                elif ns is not None:
                    reexports[_t(ns.named_children[-1])] = (src_spec, None)
                else:
                    stars.append(src_spec)
            elif decl is not None:
                if decl.type in FUNC_DECL:
                    nm = _t(decl.child_by_field_name('name'))
                    exports['default' if is_default else nm] = nm
                elif decl.type in CLASS_DECL:
                    nm = add_class(decl)
                    if nm:
                        exports['default' if is_default else nm] = nm
                elif decl.type in ('lexical_declaration', 'variable_declaration'):
                    for d in decl.named_children:
                        if d.type == 'variable_declarator':
                            nm = add_var(d)
                            if nm:
                                exports[nm] = nm
            elif is_default:
                val = n.child_by_field_name('value') or (n.named_children[-1] if n.named_children else None)
                if val is not None:
                    if val.type == 'identifier':
                        exports['default'] = _t(val)
                    elif val.type in ('function_expression', 'arrow_function', 'class', 'function'):
                        nm = val.child_by_field_name('name')
                        key = _t(nm) if nm is not None else 'default'
                        defs[key] = line
                        exports['default'] = key
            else:
                for c in n.named_children:
                    if c.type == 'export_clause':
                        for s in c.named_children:
                            if s.type != 'export_specifier' or _is_type_only(s):
                                continue
                            nm, al = s.child_by_field_name('name'), s.child_by_field_name('alias')
                            exports[_t(al or nm)] = _t(nm)
                    elif c.type == 'identifier' and any(x.type == '=' for x in n.children):
                        exports['='] = _t(c)                     # export = f
        elif t == 'call_expression':
            fn = n.child_by_field_name('function')
            if fn is not None:
                if fn.type == 'identifier':
                    calls.append({'line': line, 'pline': line, 'kind': 'bare', 'name': _t(fn)})
                elif fn.type == 'member_expression':
                    obj, prop = fn.child_by_field_name('object'), fn.child_by_field_name('property')
                    if obj is not None and obj.type == 'identifier' and prop is not None:
                        calls.append({'line': line, 'pline': prop.start_point[0] + 1, 'kind': 'member',
                                      'recv': _t(obj), 'name': _t(prop)})
        for c in n.children:
            walk(c)

    walk(tree.root_node)
    return {'defs': defs, 'imports': imports, 'exports': exports, 'reexports': reexports, 'stars': stars,
            'calls': calls}



# ---- workspace packages and tsconfig paths (same text in lib/ts_callers_ref.py and fork_ts_resolver.py) ----
def _strip_jsonc(text):
    """tsconfig is JSON with comments and trailing commas."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
        elif ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r',(\s*[}\]])', r'\1', ''.join(out))


def _repo_root(files):
    d = os.path.commonpath(files) if files else '/'
    if os.path.isfile(d):
        d = os.path.dirname(d)
    probe = d
    while probe and probe != os.path.dirname(probe):
        if os.path.isdir(os.path.join(probe, '.git')):
            return probe
        probe = os.path.dirname(probe)
    return d


class Workspace:
    """Non-relative TS imports that still name a repo file:

    - tsconfig ``paths`` (nearest tsconfig.json upward from the importing file, ``extends`` followed, relative or
      through a workspace package), mapped against ``baseUrl`` (or the defining tsconfig's folder), and plain
      ``baseUrl`` lookups;
    - workspace packages: a repo ``package.json`` whose ``name`` is unique in the repo; ``@scope/pkg`` resolves
      through ``exports`` (string, pattern ``./x/*``, or condition objects: used only when exactly one condition
      names an existing repo source file), then ``main``/``types``, then ``index``; ``@scope/pkg/sub/path``
      through ``exports`` or ``<package dir>/sub/path``.

    A spec that resolves to no repo file, or to more than one, is not resolved (left out of the certain set).
    """

    EXT = ('', '.ts', '.tsx', '.mts', '.cts', '/index.ts', '/index.tsx')

    def __init__(self, root, fileset):
        self.root, self.fileset = root, fileset
        self.pkgs, dup = {}, set()
        for d, dirs, fs in os.walk(root):
            dirs[:] = [x for x in dirs if x not in ('node_modules', '.git', 'dist', 'build', '.next', '.turbo')]
            if 'package.json' in fs:
                try:
                    with open(os.path.join(d, 'package.json'), encoding='utf-8') as fh:
                        pj = json.load(fh)
                except (OSError, ValueError):
                    continue
                nm = pj.get('name') if isinstance(pj, dict) else None
                if isinstance(nm, str) and nm:
                    if nm in self.pkgs:
                        dup.add(nm)
                    self.pkgs[nm] = (d, pj)
        for nm in dup:
            self.pkgs.pop(nm, None)
        self._conf, self._near, self._cache = {}, {}, {}

    # -- files --
    def _file(self, base):
        cands = [base + e for e in self.EXT]
        m = re.match(r'(.*)\.(js|jsx|mjs|cjs)$', base)
        if m:
            stem, ext = m.groups()
            cands += [stem + {'js': '.ts', 'jsx': '.tsx', 'mjs': '.mts', 'cjs': '.cts'}[ext], stem + '.tsx']
        for c in cands:
            if c in self.fileset:
                return c
        return None

    # -- tsconfig --
    def _load(self, path, seen=0):
        """(paths, paths_dir, baseUrl_dir) of a tsconfig, after its extends chain."""
        if path in self._conf:
            return self._conf[path]
        self._conf[path] = (None, None, None)
        try:
            with open(path, encoding='utf-8') as fh:
                cfg = json.loads(_strip_jsonc(fh.read()))
        except (OSError, ValueError):
            return self._conf[path]
        paths = pdir = bdir = None
        ext = cfg.get('extends') if isinstance(cfg, dict) else None
        for e in ([ext] if isinstance(ext, str) else ext if isinstance(ext, list) else []):
            ep = self._extends_path(e, os.path.dirname(path))
            if ep and seen < 6:
                p2, pd2, bd2 = self._load(ep, seen + 1)
                paths, pdir, bdir = p2 or paths, pd2 or pdir, bd2 or bdir
        co = cfg.get('compilerOptions') or {} if isinstance(cfg, dict) else {}
        d = os.path.dirname(path)
        if isinstance(co.get('baseUrl'), str):
            bdir = os.path.normpath(os.path.join(d, co['baseUrl']))
        if isinstance(co.get('paths'), dict):
            paths, pdir = co['paths'], d
        self._conf[path] = (paths, pdir, bdir)
        return self._conf[path]

    def _extends_path(self, e, d):
        if e.startswith('.'):
            p = os.path.normpath(os.path.join(d, e))
        else:
            name, sub = self._split(e)
            if name not in self.pkgs:
                return None
            p = os.path.join(self.pkgs[name][0], sub) if sub else os.path.join(self.pkgs[name][0], 'tsconfig.json')
        for c in (p, p + '.json'):
            if os.path.isfile(c):
                return c
        return None

    def _nearest(self, d):
        if d in self._near:
            return self._near[d]
        probe, hit = d, None
        while True:
            c = os.path.join(probe, 'tsconfig.json')
            if os.path.isfile(c):
                hit = c
                break
            if probe == self.root or probe == os.path.dirname(probe) or not probe.startswith(self.root):
                break
            probe = os.path.dirname(probe)
        self._near[d] = hit
        return hit

    # -- packages --
    @staticmethod
    def _split(spec):
        parts = spec.split('/')
        k = 2 if spec.startswith('@') else 1
        return '/'.join(parts[:k]), '/'.join(parts[k:])

    def _targets(self, val):
        """String leaves of an exports value (condition objects flattened)."""
        if isinstance(val, str):
            return [val]
        if isinstance(val, dict):
            return [s for v in val.values() for s in self._targets(v)]
        if isinstance(val, list):
            return [s for v in val for s in self._targets(v)]
        return []

    def _one(self, d, rels):
        hits = {h for h in (self._file(os.path.normpath(os.path.join(d, r))) for r in rels if isinstance(r, str)) if h}
        return next(iter(hits)) if len(hits) == 1 else None

    def _package(self, spec):
        name, sub = self._split(spec)
        if name not in self.pkgs:
            return None
        d, pj = self.pkgs[name]
        exp = pj.get('exports')
        key = './' + sub if sub else '.'
        if isinstance(exp, (str, list)) or (isinstance(exp, dict) and exp and not any(k.startswith('.') for k in exp)):
            exp = {'.': exp}                                   # "exports": "./x" or a bare condition object
        if isinstance(exp, dict):
            if key in exp:
                return self._one(d, self._targets(exp[key]))
            for k, v in exp.items():                           # pattern "./lib/*"
                if '*' in k:
                    pre, post = k.split('*', 1)
                    if key.startswith(pre) and key.endswith(post) and len(key) >= len(pre) + len(post):
                        star = key[len(pre):len(key) - len(post)]
                        return self._one(d, [t.replace('*', star) for t in self._targets(v)])
            if sub:
                return None                                    # exports present: other subpaths are blocked
        if not sub:
            return self._one(d, [x for x in (pj.get('main'), pj.get('types'), pj.get('typings')) if x] or ['index']) \
                or self._file(os.path.join(d, 'index'))
        return self._file(os.path.normpath(os.path.join(d, sub)))

    # -- entry --
    def resolve(self, spec, from_file):
        d = os.path.dirname(from_file)
        key = (d, spec)
        if key in self._cache:
            return self._cache[key]
        hit = None
        conf = self._nearest(d)
        if conf:
            paths, pdir, bdir = self._load(conf)
            if isinstance(paths, dict):
                best = None
                for pat, tgts in paths.items():
                    if pat == spec:
                        best = (len(pat) + 1, '', tgts)
                    elif pat.count('*') == 1:
                        pre, post = pat.split('*')
                        if spec.startswith(pre) and spec.endswith(post) and len(spec) >= len(pre) + len(post):
                            if best is None or len(pre) > best[0]:
                                best = (len(pre), spec[len(pre):len(spec) - len(post)], tgts)
                if best is not None:
                    base = bdir or pdir
                    hits = {h for h in (self._file(os.path.normpath(os.path.join(base, t.replace('*', best[1]))))
                                        for t in (best[2] if isinstance(best[2], list) else []) if isinstance(t, str)) if h}
                    if len(hits) == 1:
                        hit = next(iter(hits))
                    elif not hits:                             # target naming a workspace package (bundler style)
                        pk = {h for h in (self._package(t.replace('*', best[1])) for t in
                                          (best[2] if isinstance(best[2], list) else [])
                                          if isinstance(t, str) and not t.startswith('.')) if h}
                        if len(pk) == 1:
                            hit = next(iter(pk))
            if hit is None and bdir:
                hit = self._file(os.path.normpath(os.path.join(bdir, spec)))
        if hit is None:
            hit = self._package(spec)
        self._cache[key] = hit
        return hit


class FileSet(set):
    """The set of TS source files, carrying the Workspace used for non-relative specs."""
    ws = None
# ---- end workspace ----


def resolve_spec(spec, from_file, fileset):
    if not spec:
        return None
    if not spec.startswith('.'):                     # workspace package / tsconfig paths (see Workspace)
        w = getattr(fileset, 'ws', None)
        return w.resolve(spec, from_file) if w is not None else None
    base = os.path.normpath(os.path.join(os.path.dirname(from_file), spec))
    cands = [base + e for e in ('', '.ts', '.tsx', '.mts', '.cts', '/index.ts', '/index.tsx')]
    m = re.match(r'(.*)\.(js|jsx|mjs|cjs)$', base)
    if m:                                       # ESM-style TS: './x.js' names './x.ts'
        stem, ext = m.groups()
        cands += [stem + {'js': '.ts', 'jsx': '.tsx', 'mjs': '.mts', 'cjs': '.cts'}[ext], stem + '.tsx']
    for c in cands:
        if c in fileset:
            return c
    return None


def export_def(P, fileset, tf, name, seen=0):
    """(file, def key) of what file tf exports as `name`, following re-exports; None if unknown or ambiguous."""
    p = P.get(tf)
    if p is None or seen > 8:
        return None
    if name in p['exports']:
        key = p['exports'][name]
        if key in p['defs']:
            return tf, key
        if key in p['imports']:
            spec, member = p['imports'][key]
            nf = resolve_spec(spec, tf, fileset)
            if nf and member not in (None, '='):
                return export_def(P, fileset, nf, member, seen + 1)
        return None
    if name in p['reexports']:
        spec, member = p['reexports'][name]
        nf = resolve_spec(spec, tf, fileset)
        return export_def(P, fileset, nf, member, seen + 1) if nf and member else None
    if name == 'default':
        return None
    hits = set()
    for spec in p['stars']:
        nf = resolve_spec(spec, tf, fileset)
        if nf:
            h = export_def(P, fileset, nf, name, seen + 1)
            if h:
                hits.add(h)
    return next(iter(hits)) if len(hits) == 1 else None


def namespace_of(P, fileset, tf, name, seen=0):
    """File behind `export * as name from` in tf (for `import {name} ...; name.f()`)."""
    p = P.get(tf)
    if p is None or seen > 8:
        return None
    if name in p['reexports'] and p['reexports'][name][1] is None:
        return resolve_spec(p['reexports'][name][0], tf, fileset)
    return None


def certain_target(P, fileset, f, c):
    """(target file, def key, spec) for one call site of file f, or None."""
    p = P[f]
    if c['kind'] == 'bare' and c['name'] in p['imports']:
        spec, imp = p['imports'][c['name']]
        tf = resolve_spec(spec, f, fileset)
        if not tf or tf == f or imp is None:
            return None
        got = export_def(P, fileset, tf, '=' if imp == '=' else imp)
        if got and '.' not in got[1]:
            return got + (spec,)
    elif c['kind'] == 'member' and c['recv'] in p['imports']:
        spec, imp = p['imports'][c['recv']]
        tf = resolve_spec(spec, f, fileset)
        if not tf or tf == f:
            return None
        if imp is None:                                   # import * as NS
            got = export_def(P, fileset, tf, c['name'])
            if got and '.' not in got[1]:
                return got + (spec,)
            return None
        ns = namespace_of(P, fileset, tf, imp) if imp not in ('default', '=') else None
        if ns and ns != f:                                # import {ns} where ns is `export * as ns`
            got = export_def(P, fileset, ns, c['name'])
            if got and '.' not in got[1]:
                return got + (spec,)
            return None
        got = export_def(P, fileset, tf, '=' if imp == '=' else imp)
        if got and '.' not in got[1]:                     # imported class / object: C.m
            mk = f'{got[1]}.{c["name"]}'
            if mk in P[got[0]]['defs']:
                return (got[0], mk, spec)
    return None



def _base(qn):
    """`file::C.m:S1a2b` -> `file::C.m` (CRG's suffix for a second definition with the same name)."""
    return re.sub(r':S[0-9a-f]+$', '', qn)


def _short(target):
    tail = target.rsplit('::', 1)[-1]
    return tail.split(':', 1)[0].rsplit('.', 1)[-1]


DROP = ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count', 'ambiguous_targets_truncated',
        'unresolved_targets', 'collision_endpoint')


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'files': 0, 'certain_calls': 0, 'calls_resolved': 0,
             'already_bound': 0, 'no_crg_edge': 0, 'no_target_node': 0, 'kept_other_node': 0}
    if not stats['enabled']:
        return stats
    files = [f for f in store.get_all_files() if is_ts_source(f)]
    if not files:
        return stats
    from tree_sitter_language_pack import get_parser
    parsers = {'ts': get_parser('typescript'), 'tsx': get_parser('tsx')}
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    fileset = FileSet(files)
    fileset.ws = Workspace(_repo_root(files), fileset)
    fix = fix_enabled()
    stats['fix_wrong_enabled'] = fix
    stats['fixed_wrong'] = 0
    P = {}
    for f in files:
        try:
            P[f] = parse_file(f, parsers)
        except (OSError, ValueError) as exc:
            logger.warning('ts resolver: cannot parse %s: %s', f, exc)
    stats['files'] = len(P)

    node_at = defaultdict(list)
    node_file = {}
    for row in conn.execute("SELECT name, qualified_name, file_path, line_start, kind FROM nodes"):
        node_file[row['qualified_name']] = row['file_path']
        if row['kind'] in ('Function', 'Class', 'Test') and row['file_path'] in fileset:
            node_at[(row['file_path'], row['line_start'])].append((row['name'], row['qualified_name']))
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        if row['file_path'] in fileset:
            edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))

    updates, done = [], set()
    for f, p in P.items():
        # per (line, name): the certain targets of every call with that name there (None = not certain).
        # Iteration 2 overrides a CRG binding only when every such call is certain and they agree.
        targets_at = defaultdict(list)
        got_of = []
        for c in p['calls']:
            got = certain_target(P, fileset, f, c)
            got_of.append(got)
            for ln in {c['line'], c['pline']}:
                targets_at[(ln, c['name'])].append(got[:2] if got else None)
        for c, got in zip(p['calls'], got_of):
            if not got:
                continue
            tf, key, spec = got
            stats['certain_calls'] += 1
            nodes = node_at.get((tf, P[tf]['defs'][key]), [])
            if len(nodes) > 1:
                want = key.rsplit('.', 1)[-1]
                nodes = [nd for nd in nodes if nd[0] == want] or nodes
            if len(nodes) != 1:
                stats['no_target_node'] += 1
                continue
            target = nodes[0][1]
            name = c['name']
            cands = [e for ln in dict.fromkeys((c['line'], c['pline'])) for e in edges_at.get((f, ln), [])
                     if _short(e[1]) == name and e[0] not in done]
            if not cands:
                stats['no_crg_edge'] += 1
                continue
            if any(e[1] == target and 'collision_endpoint' not in (e[2] or '') for e in cands):
                stats['already_bound'] += 1
                continue
            for eid, tq, extra in cands:
                rule_used = RULE
                if tq in node_file and _base(tq) != _base(target) and not (
                        c['kind'] == 'member' and node_file[tq] == f):
                    agree = {tuple(x) if x else None for ln in {c['line'], c['pline']}
                             for x in targets_at[(ln, name)]}
                    if not (fix and agree == {(tf, key)}):
                        stats['kept_other_node'] += 1
                        continue    # CRG bound it to another real node; not ours to override (Iteration 1)
                    rule_used = FIX_RULE    # Iteration 2: syntax fixes the target, CRG's node is wrong
                    stats['fixed_wrong'] += 1
                d = json.loads(extra or '{}') or {}
                for k in DROP:
                    d.pop(k, None)
                tag = {**d, 'rule': rule_used, 'ts_import': spec}
                if rule_used == FIX_RULE:
                    tag['collision_evidence'] = FIX_RULE
                    tag['replaced_target'] = tq
                updates.append((target, json.dumps(tag), eid))
                done.add(eid)
                break

    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('TS import resolution: %s', stats)
    return stats
