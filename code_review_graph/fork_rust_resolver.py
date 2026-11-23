"""Post-build pass that binds Rust calls whose target is fixed by module paths and `use` (our fork).

Stock CRG leaves most cross-file Rust calls unbound (notebooks/14-rust): `f()` for a `use`d `f` stays a bare
UNRESOLVED name, `m::f()` / `crate::a::f()` / `Type::new()` keep the path as written as their target, and the parser
sometimes claims a same-file target that does not exist (`claimed_endpoint`).

This pass rebuilds each crate's module tree (Cargo.toml roots + `mod x;` / inline `mod x {}` / `#[path]`) and its
`use` declarations (`pub use` re-exports and unique `use x::*` glob hits followed), then for every call whose target
is certain from syntax alone:

- `f()` for a `use`d function, `m::f()`, `crate::/super::/self::` paths, `other_crate::a::f()` (workspace crates)
- `Type::f()` / `Self::f()` when Type is defined once in its crate and its inherent impls define exactly one `f`
  (not when a trait impl of Type also defines `f`)

rewrites the CRG CALLS edge on that line (or on the called name's line) with that name to the target's CRG node,
only when the edge's target is not already a real node (or is a same-name `:S<hash>` duplicate of it). It drops
`claimed_endpoint`, `ambiguous_*` and `collision_endpoint` from edges it binds. Nothing is invented: a call with no
CRG edge stays without one. Method calls on values (`x.m()`) need types and are never touched.

Every rewritten edge gets `extra.rule = 'rust.use-resolver'` and `extra.rust_path` (the call path as written).

Iteration 2, rule `rust.fix-wrong`: when the certain target is known but CRG (its scoped_resolver "single_match",
or the bare-name pass) already bound the edge to a DIFFERENT real node -- `SQLiteDatabase::new()` -> `Path.new`,
a bare `from_str()` -> a same-file method, which Rust name lookup cannot reach -- the edge is rewritten to the
certain target and tagged `rule = 'rust.fix-wrong'`, `collision_evidence = 'rust.fix-wrong'`. An edge whose target
is the certain target of another call on the same line is never touched (it is right for that call).
Both rules carry `collision_evidence`, so CRG's collision pass (graph.py, rule `crg.keep-resolver-evidence`) keeps
them instead of turning a binding to a duplicated name back into AMBIGUOUS.
Switch: rule name `rust.use-resolver`; off with CRG_RULES=off or CRG_RULES_OFF=rust.use-resolver; with
CRG_RULES_ONLY set it runs only when named there. The module/`use` logic is the same as lib/rust_callers_ref.py.
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

RULE = 'rust.use-resolver'
FIX_RULE = 'rust.fix-wrong'


def enabled() -> bool:
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return False
    names = lambda v: {s.strip() for s in os.environ.get(v, '').split(',') if s.strip()}  # noqa: E731
    only, off = names('CRG_RULES_ONLY'), names('CRG_RULES_OFF')
    if only:
        return RULE in only
    return RULE not in off


TYPE_ITEMS = ('struct_item', 'enum_item', 'union_item', 'type_item', 'trait_item')
SKIP_DIRS = {'target', '.git', 'node_modules'}


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _segs(text):
    """'a::b::<T>::c' / 'Vec::<u8>::new' / '<T as X>::f' -> ['a', 'b', 'c'] (generic args removed)."""
    text = re.sub(r'\s+', '', text)
    prev = None
    while prev != text:                       # strip nested <...>
        prev = text
        text = re.sub(r'<[^<>]*>', '', text)
    return [s for s in text.split('::') if s]


class Module:
    __slots__ = ('crate', 'path', 'file', 'dir', 'parent', 'children', 'fns', 'types', 'uses', 'globs')

    def __init__(self, crate, path, file, dir_, parent):
        self.crate, self.path, self.file, self.dir, self.parent = crate, path, file, dir_, parent
        self.children, self.fns, self.types, self.uses, self.globs = {}, {}, {}, {}, []


def _cargo_crates(root):
    """[(crate name, root file)] from every Cargo.toml under root."""
    out = []
    for toml in Path(root).rglob('Cargo.toml'):
        if SKIP_DIRS & set(toml.parts):
            continue
        txt = toml.read_text(errors='replace')
        d = toml.parent
        sec, pkg, lib_name, lib_path, bins = None, None, None, None, []
        cur = {}
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith('['):
                if sec == '[[bin]]' and cur.get('path'):
                    bins.append(cur['path'])
                sec, cur = s, {}
                continue
            m = re.match(r'(name|path)\s*=\s*"([^"]+)"', s)
            if m:
                if sec == '[package]' and m.group(1) == 'name':
                    pkg = m.group(2)
                elif sec == '[lib]':
                    if m.group(1) == 'name':
                        lib_name = m.group(2)
                    else:
                        lib_path = m.group(2)
                elif sec == '[[bin]]':
                    cur[m.group(1)] = m.group(2)
        if sec == '[[bin]]' and cur.get('path'):
            bins.append(cur['path'])
        if not pkg:
            continue
        name = (lib_name or pkg).replace('-', '_')
        lib = d / (lib_path or 'src/lib.rs')
        if lib.exists():
            out.append((name, str(lib)))
        roots = [d / 'src/main.rs', *(d / b for b in bins)]
        for sub in ('src/bin', 'tests', 'examples', 'benches'):
            sd = d / sub
            if sd.is_dir():
                roots += [p for p in sd.glob('*.rs')] + [p for p in sd.glob('*/main.rs')]
        for r in roots:
            if r.exists():
                out.append((pkg.replace('-', '_') + ':' + r.stem, str(r)))   # bin/test crates: own name
    return out


class Repo:
    def __init__(self, root):
        self.root = str(root)
        from tree_sitter_language_pack import get_parser
        self.parser = get_parser('rust')
        self.trees, self.src = {}, {}
        self.crates = {}              # crate name -> root Module (library crates by their importable name)
        self.file_mod = {}            # file -> first Module whose file it is (top of that file)
        self.impls = defaultdict(lambda: defaultdict(list))   # (crate) -> type name -> [(fn, file, line)]
        self.trait_impl_fns = defaultdict(set)                  # (crate, type) -> fn names from trait impls
        self.type_count = defaultdict(int)                      # (crate, type name) -> definitions in crate
        self.method_names = defaultdict(set)                   # fn name -> files defining it in an impl
        self.mods = []
        self._memo = {}
        for name, rootfile in _cargo_crates(root):
            if rootfile in self.file_mod:
                m = self.file_mod[rootfile]
                if ':' not in name:
                    self.crates.setdefault(name, m)
                continue
            m = Module(name, (), rootfile, os.path.dirname(rootfile), None)
            if ':' not in name:
                self.crates.setdefault(name, m)
            self._load(m, rootfile, is_dir_owner=True)

    def tree(self, f):
        if f not in self.trees:
            self.src[f] = Path(f).read_bytes()
            self.trees[f] = self.parser.parse(self.src[f])
        return self.trees[f]

    def _load(self, mod, f, is_dir_owner):
        if f in self.file_mod:
            return
        self.file_mod[f] = mod
        try:
            tree = self.tree(f)
        except OSError:
            return
        base = os.path.basename(f)
        if is_dir_owner or base in ('mod.rs', 'lib.rs', 'main.rs'):
            mod.dir = os.path.dirname(f)
        else:
            mod.dir = os.path.join(os.path.dirname(f), base[:-3])
        self._items(mod, tree.root_node, f)

    def _items(self, mod, body, f):
        self.mods.append(mod)
        prev_attr = []
        for n in body.named_children:
            if n.type == 'attribute_item':
                prev_attr.append(_t(n))
                continue
            attrs, prev_attr = prev_attr, []
            if n.type == 'mod_item':
                name = _t(n.child_by_field_name('name'))
                child = Module(mod.crate, mod.path + (name,), f, None, mod)
                mod.children[name] = child
                b = n.child_by_field_name('body')
                if b is not None:                          # inline module, same file
                    child.dir = os.path.join(mod.dir, name)
                    self._items(child, b, f)
                    continue
                pm = next((re.search(r'path\s*=\s*"([^"]+)"', a) for a in attrs if 'path' in a), None)
                # normpath: `#[path = "../x/y.rs"]` must give the same file name as the crate that owns x/y.rs
                cands = ([os.path.normpath(os.path.join(os.path.dirname(f), pm.group(1)))]
                         if pm else [os.path.join(mod.dir, name + '.rs'), os.path.join(mod.dir, name, 'mod.rs')])
                hit = next((c for c in cands if os.path.isfile(c)), None)
                if hit:
                    child.file = hit
                    self._load(child, hit, is_dir_owner=False)
            elif n.type == 'function_item':
                mod.fns[_t(n.child_by_field_name('name'))] = (f, n.start_point[0] + 1)
            elif n.type in TYPE_ITEMS:
                nm = _t(n.child_by_field_name('name'))
                mod.types[nm] = (f, n.start_point[0] + 1)
                self.type_count[(mod.crate, nm)] += 1
            elif n.type == 'impl_item':
                ty = _segs(_t(n.child_by_field_name('type')))
                b = n.child_by_field_name('body')
                if not ty or b is None:
                    continue
                tname = ty[-1]
                trait = n.child_by_field_name('trait') is not None
                for it in b.named_children:
                    if it.type == 'function_item':
                        fn = _t(it.child_by_field_name('name'))
                        self.method_names[fn].add(f)
                        if trait:
                            self.trait_impl_fns[(mod.crate, tname)].add(fn)
                        else:
                            self.impls[mod.crate][tname].append((fn, f, it.start_point[0] + 1))
            elif n.type == 'use_declaration':
                arg = n.child_by_field_name('argument')
                if arg is not None:
                    self._use(mod, arg, [])
            elif n.type == 'foreign_mod_item' or n.type == 'macro_invocation':
                pass

    def _use(self, mod, n, prefix):
        t = n.type
        if t in ('identifier', 'scoped_identifier', 'crate', 'self', 'super'):
            segs = prefix + _segs(_t(n))
            if segs and segs[-1] == 'self':
                segs = segs[:-1]
            if segs:
                mod.uses[segs[-1]] = segs
        elif t == 'use_as_clause':
            p, a = n.child_by_field_name('path'), n.child_by_field_name('alias')
            segs = prefix + _segs(_t(p))
            if segs and segs[-1] == 'self':
                segs = segs[:-1]
            if segs and a is not None and _t(a) != '_':
                mod.uses[_t(a)] = segs
        elif t == 'scoped_use_list':
            p, lst = n.child_by_field_name('path'), n.child_by_field_name('list')
            pre = prefix + (_segs(_t(p)) if p is not None else [])
            if lst is not None:
                self._use(mod, lst, pre)
        elif t == 'use_list':
            for c in n.named_children:
                self._use(mod, c, prefix)
        elif t == 'use_wildcard':
            inner = [c for c in n.named_children]
            mod.globs.append(prefix + (_segs(_t(inner[0])) if inner else []))

    # ---- name resolution --------------------------------------------------------------------------------
    def _lookup(self, mod, name, depth):
        """What `name` means inside mod: ('mod', Module) | ('fn', (file, line)) | ('type', (crate, name)) | None."""
        if depth > 8:
            return None
        key = (id(mod), name)
        if key in self._memo:
            return self._memo[key]
        r = self._lookup_uncached(mod, name, depth)
        if r is not None:          # a None may come from the depth cut-off, so only hits are remembered
            self._memo[key] = r
        return r

    def _lookup_uncached(self, mod, name, depth):
        if name in mod.children:
            return ('mod', mod.children[name])
        if name in mod.fns:
            return ('fn', mod.fns[name])
        if name in mod.types:
            return ('type', (mod.crate, name))
        if name in mod.uses:
            return self.resolve(mod, mod.uses[name], depth + 1, in_use=True)
        hits = set()
        for g in mod.globs:
            tgt = self.resolve(mod, g, depth + 1, in_use=True)
            if tgt and tgt[0] == 'mod':
                r = self._lookup(tgt[1], name, depth + 1)
                if r:
                    hits.add((r[0], r[1] if r[0] != 'mod' else id(r[1]), r[1] if r[0] == 'mod' else None))
        if len(hits) == 1:
            k, v, m = next(iter(hits))
            return ('mod', m) if k == 'mod' else (k, v)
        return None

    def resolve(self, mod, segs, depth=0, in_use=False, self_type=None):
        if not segs or depth > 8:
            return None
        first, rest = segs[0], segs[1:]
        cur = None
        if first == 'crate':
            cur = mod
            while cur.parent is not None:
                cur = cur.parent
            cur = ('mod', cur)
        elif first == 'self':
            cur = ('mod', mod)
        elif first == 'super':
            cur = ('mod', mod.parent) if mod.parent is not None else None
            while cur and rest and rest[0] == 'super':
                rest = rest[1:]
                cur = ('mod', cur[1].parent) if cur[1].parent is not None else None
        elif first == 'Self':
            cur = ('type', self_type) if self_type else None
        else:
            cur = self._lookup(mod, first, depth)
            if cur is None and first in self.crates:
                cur = ('mod', self.crates[first])
        for i, s in enumerate(rest):
            if cur is None:
                return None
            if cur[0] == 'mod':
                cur = self._lookup(cur[1], s, depth + 1)
            elif cur[0] == 'type' and i == len(rest) - 1:
                return ('assoc', cur[1], s)
            else:
                return None
        return cur

    def assoc_fn(self, crate, tname, fn):
        if self.type_count[(crate, tname)] != 1:
            return None
        hits = [(f, line) for name, f, line in self.impls[crate].get(tname, []) if name == fn]
        if len(hits) != 1 or fn in self.trait_impl_fns[(crate, tname)]:
            return None
        return hits[0]


def reference(root):
    """Parse the repo; return (Repo, certain call sites, method-call sites). Cached per root: the reference
    depends only on the source, not on the graph being scored."""
    return _reference(str(root))


def _reference(root):
    R = Repo(root)
    certain, methods = [], []
    seen_files = set()
    for mod in list(R.mods):
        f = mod.file
        if f is None or (f, mod.path) in seen_files:
            continue
        seen_files.add((f, mod.path))
    # walk each file once, tracking the inline module and impl type at each call
    for f, top in list(R.file_mod.items()):
        tree = R.tree(f)
        stack = [(tree.root_node, top, None)]
        while stack:
            n, mod, impl_ty = stack.pop()
            t = n.type
            if t == 'mod_item' and n.child_by_field_name('body') is not None:
                child = mod.children.get(_t(n.child_by_field_name('name')))
                if child is not None:
                    stack.extend((c, child, None) for c in n.child_by_field_name('body').children)
                continue
            if t == 'impl_item':
                ty = _segs(_t(n.child_by_field_name('type')))
                if ty:
                    r = R.resolve(mod, ty)
                    impl_ty = r[1] if r and r[0] == 'type' else None
            if t == 'call_expression':
                fn = n.child_by_field_name('function')
                if fn is not None and fn.type == 'generic_function':
                    fn = fn.child_by_field_name('function')
                line = n.start_point[0] + 1
                if fn is not None and fn.type in ('identifier', 'scoped_identifier'):
                    segs = _segs(_t(fn))
                    name_node = fn.child_by_field_name('name') if fn.type == 'scoped_identifier' else fn
                    pline = name_node.start_point[0] + 1 if name_node is not None else line
                    r = R.resolve(mod, segs, self_type=impl_ty)
                    tgt = None
                    if r and r[0] == 'fn':
                        tgt = r[1]
                    elif r and r[0] == 'assoc':
                        tgt = R.assoc_fn(r[1][0], r[1][1], r[2])
                    if tgt and tgt[0] != f:
                        certain.append({'file': f, 'line': line, 'pline': pline, 'name': segs[-1],
                                        'target_file': tgt[0], 'target_line': tgt[1], 'path': '::'.join(segs)})
                elif fn is not None and fn.type == 'field_expression':
                    fld = fn.child_by_field_name('field')
                    nm = _t(fld)
                    if R.method_names.get(nm, set()) - {f}:
                        methods.append({'file': f, 'line': line, 'name': nm})
            stack.extend((c, mod, impl_ty) for c in n.children)
    return R, certain, methods


def _repo_root(files):
    start = os.path.commonpath([os.path.dirname(f) for f in files])
    cur = start
    for _ in range(8):
        if os.path.isfile(os.path.join(cur, 'Cargo.toml')) or os.path.isdir(os.path.join(cur, '.git')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return start


def _base(qn):
    return re.sub(r':S[0-9a-f]+$', '', qn)


def _short(target):
    tail = target.rsplit('::', 1)[-1]
    return tail.split(':', 1)[0].rsplit('.', 1)[-1]


def fix_enabled() -> bool:
    try:
        from .rules import switch_on
    except ImportError:
        return False
    return switch_on(FIX_RULE)


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'fix_enabled': False, 'files': 0, 'certain_calls': 0, 'calls_resolved': 0,
             'already_bound': 0, 'no_crg_edge': 0, 'no_target_node': 0, 'bound_elsewhere': 0, 'wrong_fixed': 0}
    if not stats['enabled']:
        return stats
    files = [f for f in store.get_all_files() if f.endswith('.rs')]
    if not files:
        return stats
    fileset = set(files)
    _, certain, _ = reference(_repo_root(files))
    fix = stats['fix_enabled'] = fix_enabled()
    stats['files'] = len(fileset)
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    node_at = defaultdict(list)
    for row in conn.execute("SELECT name, qualified_name, file_path, line_start FROM nodes "
                            "WHERE kind IN ('Function','Test')"):
        if row['file_path'] in fileset:
            node_at[(row['file_path'], row['line_start'])].append((row['name'], row['qualified_name']))
    node_names = {r[0] for r in conn.execute('SELECT qualified_name FROM nodes')}
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        if row['file_path'] in fileset:
            edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))
    # certain targets per call line: an edge bound to one of these is right for some call on that line
    line_targets = defaultdict(set)
    for c in certain:
        for nd in node_at.get((c['target_file'], c['target_line']), []):
            line_targets[(c['file'], c['line'])].add(nd[1])
            line_targets[(c['file'], c['pline'])].add(nd[1])
    updates, done = [], set()
    for c in certain:
        if c['file'] not in fileset or c['target_file'] not in fileset:
            continue
        stats['certain_calls'] += 1
        nodes = node_at.get((c['target_file'], c['target_line']), [])
        if len(nodes) > 1:
            nodes = [nd for nd in nodes if nd[0] == c['name']] or nodes
        if len(nodes) != 1:
            stats['no_target_node'] += 1
            continue
        target = nodes[0][1]
        cands = [e for ln in dict.fromkeys((c['line'], c['pline'])) for e in edges_at.get((c['file'], ln), [])
                 if _short(e[1]) == c['name'] and e[0] not in done]
        if not cands:
            stats['no_crg_edge'] += 1
            continue
        if any(e[1] == target and 'collision_endpoint' not in (e[2] or '') for e in cands):
            stats['already_bound'] += 1
            continue
        for eid, tq, extra in cands:
            if tq in node_names and _base(tq) != _base(target):
                continue    # CRG bound it to another real node; not ours to override
            d = json.loads(extra or '{}') or {}
            for k in ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count',
                      'ambiguous_targets_truncated', 'unresolved_targets', 'collision_endpoint'):
                d.pop(k, None)
            updates.append((target, json.dumps({**d, 'rule': RULE, 'rust_path': c['path'],
                                                'collision_evidence': RULE}), eid))
            done.add(eid)
            break
        else:
            stats['bound_elsewhere'] += 1
            if fix:     # Iteration 2: CRG bound it to another real node; the use/module syntax says otherwise
                taken = line_targets[(c['file'], c['line'])] | line_targets[(c['file'], c['pline'])]
                for eid, tq, extra in cands:
                    if tq in taken:
                        continue
                    d = json.loads(extra or '{}') or {}
                    for k in ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count',
                              'ambiguous_targets_truncated', 'unresolved_targets', 'collision_endpoint'):
                        d.pop(k, None)
                    updates.append((target, json.dumps({**d, 'rule': FIX_RULE, 'rust_path': c['path'],
                                                        'replaced_target': tq,
                                                        'collision_evidence': FIX_RULE}), eid))
                    done.add(eid)
                    stats['wrong_fixed'] += 1
                    break
    if updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('Rust use resolution: %s', stats)
    return stats
