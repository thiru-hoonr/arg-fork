"""Post-build pass that resolves Lua ``require`` across files (our fork; prototype).

Stock CRG stores ``require("lazy.core.util")`` as an IMPORTS_FROM edge whose target is the raw string
``lazy.core.util`` and stores ``Util.track()`` as a CALLS edge whose target is the bare name ``track``.
Neither points at a file, so CRG's binding rule ("same file or exactly one imported file") never fires
and every cross-file Lua call stays UNRESOLVED.

What this pass does, after the build, on the stored graph:

1. Module -> file. ``a.b.c`` maps to a repo file ending in ``/a/b/c.lua`` or ``/a/b/c/init.lua``.
   Used only when exactly one file matches (a module name shared by two files is left alone).
2. IMPORTS_FROM targets that are such module names are rewritten to that file's path, so
   ``importers_of`` and impact radius see the dependency.
3. Aliases per file: ``local X = require("a.b.c")`` (also ``require "a.b.c"`` and ``require('a.b.c')``).
4. CALLS edges with a bare target ``f`` whose source line contains ``X.f(`` / ``X:f(`` / ``X.f {`` /
   ``X.f "`` for an alias X are rewritten to the node in X's file named ``f``, only when that file has
   exactly one Function node with that name. Otherwise the edge stays as it was.

5. Guard. Once imports point at files, CRG's own post-build pass (``resolve_bare_call_targets``) binds any
   bare name that one imported file defines, so the builtin ``error("x")`` became ``Util.error`` (264 wrong
   edges on lazy.nvim, notebooks/09). A bare call this pass could not bind, whose name a resolved imported
   file defines but the calling file does not, is marked ``unresolved_targets: []`` (the marker CRG's own
   csharp_resolver uses for "checked, leave it"), so CRG does not guess it. Same-file binding is untouched.

Every rewritten edge gets ``extra.rule = 'lua.require-resolver'`` and ``extra.lua_require`` (the module),
so notebooks can count them. Nothing is ever deleted or invented: an edge that is not certain stays bare.

Switch: rule name ``lua.require-resolver``. Off with ``CRG_RULES=off`` or ``CRG_RULES_OFF=lua.require-resolver``;
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

RULE = 'lua.require-resolver'
INLINE = re.compile(r'''require\s*\(?\s*["']([\w./-]+)["']\s*\)?\s*[.:]''')
REQUIRE = re.compile(r'''\blocal\s+([A-Za-z_]\w*)\s*=\s*require\s*\(?\s*["']([\w./-]+)["']''')


def enabled() -> bool:
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return False
    names = lambda v: {s.strip() for s in os.environ.get(v, '').split(',') if s.strip()}
    only, off = names('CRG_RULES_ONLY'), names('CRG_RULES_OFF')
    if only:
        return RULE in only
    return RULE not in off


def _module_index(lua_files):
    """Module name -> file, only for names that map to exactly one file."""
    by_mod = defaultdict(set)
    for fp in lua_files:
        parts = fp.replace('\\', '/').split('/')
        stem = parts[-1][:-4]
        dirs = parts[:-1]
        if stem == 'init':
            stem, dirs = dirs[-1], dirs[:-1]
        # every suffix of the path is a candidate module name: c, b.c, a.b.c, ...
        chain = [stem]
        for d in reversed(dirs[-6:]):
            chain.insert(0, d)
            by_mod['.'.join(chain)].add(fp)
        by_mod[stem].add(fp)
    return {m: next(iter(fs)) for m, fs in by_mod.items() if len(fs) == 1}


def resolve_lua_requires(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'modules': 0, 'imports_resolved': 0, 'calls_resolved': 0}
    if not stats['enabled']:
        return stats
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    lua_files = [f for f in store.get_all_files() if f.lower().endswith('.lua')]
    if not lua_files:
        return stats
    mod2file = _module_index(lua_files)
    stats['modules'] = len(mod2file)

    # Function nodes per file, by short name; only unique names are bindable.
    funcs = defaultdict(lambda: defaultdict(list))
    for row in conn.execute("SELECT name, qualified_name, file_path FROM nodes WHERE kind='Function'"):
        if row['file_path'] in mod2file.values():
            funcs[row['file_path']][row['name']].append(row['qualified_name'])

    lua_set = set(lua_files)
    def tag(extra, mod):
        d = json.loads(extra or '{}') or {}
        d.pop('claimed_endpoint', None)          # the receiver says which file; a same-file claim is wrong
        return json.dumps({**d, 'rule': RULE, 'lua_require': mod})
    node_names = {r[0] for r in conn.execute('SELECT qualified_name FROM nodes')}

    # 2. IMPORTS_FROM -> file path
    imp_updates = []
    for row in conn.execute("SELECT id, target_qualified, file_path, extra FROM edges WHERE kind='IMPORTS_FROM'"):
        if row['file_path'] in lua_set and row['target_qualified'] in mod2file:
            imp_updates.append((mod2file[row['target_qualified']], tag(row['extra'], row['target_qualified']), row['id']))

    # 3. aliases and source lines per file
    aliases, lines = {}, {}
    for fp in lua_files:
        try:
            with open(fp, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except OSError:
            continue
        lines[fp] = text.splitlines()
        aliases[fp] = {a: m for a, m in REQUIRE.findall(text) if m in mod2file}

    # names defined per Lua file (any kind of function), for the guard
    defined = defaultdict(set)
    for row in conn.execute("SELECT name, file_path FROM nodes WHERE kind='Function'"):
        defined[row['file_path']].add(row['name'])
    imported = defaultdict(set)
    for row in conn.execute("SELECT target_qualified, file_path FROM edges WHERE kind='IMPORTS_FROM'"):
        if row['file_path'] in lua_set and row['target_qualified'] in mod2file:
            imported[row['file_path']].add(mod2file[row['target_qualified']])

    types = _LuaTypes(lua_files, node_names, aliases, mod2file) if ann_enabled() else None
    stats['annotation_enabled'] = types is not None
    ann_updates = []

    # 4. CALLS
    call_updates, guard_updates = [], []
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        fp, name = row['file_path'], row['target_qualified']
        if fp not in lines or not row['line']:
            continue
        if '::' in name:
            # The parser may claim a same-file target (`init.lua::setup` for `Config.setup()`) that names no
            # node; CRG later turns it bare. Such a claim is ours to check; a real node is left alone.
            if name in node_names or name.split('::', 1)[0] != fp:
                continue
            name = name.rsplit('::', 1)[1]
        src = lines[fp][row['line'] - 1] if 0 < row['line'] <= len(lines[fp]) else ''
        hits = set()
        for alias, mod in aliases.get(fp, {}).items():
            if re.search(rf'(?<![\w.]){re.escape(alias)}\s*[.:]\s*{re.escape(name)}\s*[({{"\']', src):
                cands = funcs[mod2file[mod]].get(name, [])
                if len(cands) == 1:
                    hits.add((cands[0], mod))
        for mod in INLINE.findall(src):          # require("a.b").f(...) written inline
            if mod in mod2file and re.search(rf'''require\s*\(?\s*["']{re.escape(mod)}["']\s*\)?\s*[.:]\s*{re.escape(name)}\s*[({{"']''', src):
                cands = funcs[mod2file[mod]].get(name, [])
                if len(cands) == 1:
                    hits.add((cands[0], mod))
        if len(hits) == 1:                       # one certain target, else leave it bare
            (target, mod), = hits
            call_updates.append((target, tag(row['extra'], mod), row['id']))
        elif types is not None and (b := types.bind(fp, row['line'], name)) is not None:
            d = json.loads(row['extra'] or '{}') or {}
            d.pop('claimed_endpoint', None)
            ann_updates.append((b[0], json.dumps({**d, 'rule': ANN_RULE, 'lua_type': b[1]}), row['id']))
        elif name not in defined[fp] and any(name in defined[f] for f in imported[fp]):
            extra = json.loads(row['extra'] or '{}') or {}
            if 'bare_call_target' not in extra:
                guard_updates.append((json.dumps({**extra, 'unresolved_targets': [], 'rule': RULE,
                                                  'lua_guard': 'no receiver match'}), row['id']))

    if imp_updates or call_updates or guard_updates or ann_updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', imp_updates + call_updates + ann_updates)
        conn.executemany('UPDATE edges SET extra = ? WHERE id = ?', guard_updates)
        store.commit()
        store._invalidate_cache()
    stats['imports_resolved'], stats['calls_resolved'] = len(imp_updates), len(call_updates)
    stats['calls_guarded'] = len(guard_updates)
    stats['calls_bound_by_annotation'] = len(ann_updates)
    logger.info('Lua require resolution: %s', stats)
    return stats


# ---------------------------------------------------------------------------------------------------------------
# Rule 2: lua.annotation-types — method calls on objects, typed by LuaLS/EmmyLua annotations (prototype)
#
# `running:suspend()` names no module, so the require rule cannot bind it. Many Lua codebases annotate types for
# the Lua language server. This pass reads them:
#   ---@class Async            above `local Async = {}`          class Async lives in that table and file
#   ---@class LazyTask: Async                                     parent class, for inherited methods
#   ---@field _running? Async                                     type of `obj._running`
#   ---@param running Async     above a function                  type of a parameter inside that function
#   ---@type Async              above or after `local v = ...`    type of a local
#   ---@return Async            above a function                  `local v = Mod.f()` / `Class.f()` gets that type
#   `self` inside `function Async:m()` / `function Async.m()` is an Async.
# A call `v:m()` / `v.m()` / `self.f:m()` is bound only when the receiver's type is one class, that class name is
# declared in exactly one file, and the class (or a parent) defines m. Otherwise nothing is written.
# ---------------------------------------------------------------------------------------------------------------
ANN_RULE = 'lua.annotation-types'
_ANN = re.compile(r'---\s*@(class|field|param|type|return)\s+(.*)')


def ann_enabled() -> bool:
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return False
    names = lambda v: {s.strip() for s in os.environ.get(v, '').split(',') if s.strip()}
    only, off = names('CRG_RULES_ONLY'), names('CRG_RULES_OFF')
    return ANN_RULE in only if only else ANN_RULE not in off


def _one_type(t):
    """'Async?' / 'Async|nil' / 'Async' -> 'Async'; unions of classes, arrays, tables, functions -> None."""
    t = t.strip().split(' ', 1)[0] if not t.strip().startswith('{') else ''
    parts = [p.strip().rstrip('?') for p in t.split('|') if p.strip() and p.strip() != 'nil']
    if len(parts) != 1 or not re.fullmatch(r'[A-Za-z_][\w.]*', parts[0]):
        return None
    return parts[0]


class _LuaTypes:
    def __init__(self, files, node_names, aliases, mod2file):
        from tree_sitter_language_pack import get_parser
        self.parser = get_parser('lua')
        self.node_names, self.aliases, self.mod2file = node_names, aliases, mod2file
        self.class_files = defaultdict(set)     # class -> {(file, table)}
        self.parent, self.fields = {}, defaultdict(dict)
        self.returns = {}                       # (file, 'Table.f') -> type
        self.table_class = {}                   # (file, table) -> class
        self.scopes = {}                        # file -> [(start, end, {var: type})], innermost last
        self.calls = defaultdict(list)          # (file, line) -> [(receiver node, method name)]
        for fp in files:
            try:
                src = open(fp, 'rb').read()
            except OSError:
                continue
            self._index(fp, src)

    # -- per file ----------------------------------------------------------------
    def _index(self, fp, src):
        text = src.decode('utf-8', 'replace')
        lines = text.splitlines()
        block, cur_class = [], None              # annotations waiting for the next code line
        pending_by_line = {}                     # code line -> annotations just above it
        for i, raw in enumerate(lines, 1):
            s = raw.strip()
            m = _ANN.search(s)
            if s.startswith('---') and m:
                kind, rest = m.groups()
                if kind == 'class':
                    name, _, par = rest.partition(':')
                    cur_class = name.strip().split()[0] if name.strip() else None
                    if cur_class and _one_type(par.split(',')[0] if par else ''):
                        self.parent[cur_class] = _one_type(par.split(',')[0])
                elif kind == 'field' and cur_class:
                    f = rest.split(None, 1)
                    if len(f) == 2:
                        self.fields[cur_class][f[0].rstrip('?')] = _one_type(f[1])
                block.append((kind, rest, cur_class))
                continue
            if s.startswith('--') or not s:
                if not s:
                    cur_class = None
                continue
            if block:
                pending_by_line[i] = block
            tm = re.search(r'---\s*@type\s+(.*)$', s)              # trailing `local x = ... ---@type T`
            if tm:
                pending_by_line.setdefault(i, []).append(('type', tm.group(1), None))
            block, cur_class = [], None
        tree = self.parser.parse(src)
        scopes = [(1, len(lines) + 1, {})]
        self.scopes[fp] = scopes
        self._walk(fp, tree.root_node, scopes[0][2], None, pending_by_line, scopes)

    def _walk(self, fp, n, scope, self_class, pend, scopes):
        t = n.type
        line = n.start_point[0] + 1
        if t == 'variable_declaration' or t == 'assignment_statement':
            anns = pend.get(line, [])
            assign = n if t == 'assignment_statement' else next((c for c in n.named_children if c.type == 'assignment_statement'), None)
            names = [c for c in (assign.named_children[0].named_children if assign is not None else n.named_children)
                     if c.type == 'identifier'] if t == 'variable_declaration' else []
            for kind, rest, cls in anns:
                if kind == 'class' and assign is not None:
                    tgt = assign.named_children[0].named_children[0] if assign.named_children[0].named_children else None
                    cname = rest.partition(':')[0].strip().split()[0]
                    if tgt is not None:
                        self.class_files[cname].add((fp, tgt.text.decode()))
                        self.table_class[(fp, tgt.text.decode())] = cname
                if kind == 'type' and names:
                    ty = _one_type(rest)
                    if ty:
                        scope[names[0].text.decode()] = ty
            if names and assign is not None and not any(k == 'type' for k, *_ in anns):
                ty = self._expr_type(fp, assign.named_children[-1].named_children[0] if assign.named_children[-1].named_children else None, scope, self_class)
                if ty:
                    scope[names[0].text.decode()] = ty
        if t in ('function_declaration', 'function_definition'):
            inner, cls = {}, self_class
            name = n.child_by_field_name('name')
            if name is not None and name.type in ('dot_index_expression', 'method_index_expression'):
                tbl = name.child_by_field_name('table')
                fld = name.child_by_field_name('field') or name.child_by_field_name('method')
                if tbl is not None:
                    cls = self.table_class.get((fp, tbl.text.decode()))
                    for kind, rest, _ in pend.get(line, []):
                        if kind == 'return' and fld is not None:
                            ty = _one_type(rest)
                            if ty:
                                self.returns[(fp, f'{tbl.text.decode()}.{fld.text.decode()}')] = ty
            for kind, rest, _ in pend.get(line, []):
                if kind == 'param':
                    p = rest.split(None, 1)
                    if len(p) == 2 and _one_type(p[1]):
                        inner[p[0].rstrip('?')] = _one_type(p[1])
            if cls:
                inner['self'] = cls
            scopes.append((line, n.end_point[0] + 1, inner))
            for c in n.children:
                self._walk(fp, c, inner, cls, pend, scopes)
            return
        if t == 'function_call':
            fn = n.child_by_field_name('name')
            if fn is not None and fn.type in ('dot_index_expression', 'method_index_expression'):
                recv = fn.child_by_field_name('table')
                fld = fn.child_by_field_name('field') or fn.child_by_field_name('method')
                if recv is not None and fld is not None:
                    self.calls[(fp, line)].append((recv, fld.text.decode()))
        for c in n.children:
            self._walk(fp, c, scope, self_class, pend, scopes)

    # -- types -------------------------------------------------------------------
    def _lookup(self, fp, line, var):
        best = None
        for start, end, binds in self.scopes.get(fp, []):
            if start <= line <= end and var in binds:
                if best is None or start >= best[0]:
                    best = (start, binds[var])
        return best[1] if best else None

    def _expr_type(self, fp, e, scope, self_class):
        """Type of `Mod.f(...)` / `Class.f(...)` from ---@return, else None."""
        if e is None or e.type != 'function_call':
            return None
        fn = e.child_by_field_name('name')
        if fn is None or fn.type not in ('dot_index_expression', 'method_index_expression'):
            return None
        tbl = fn.child_by_field_name('table')
        fld = fn.child_by_field_name('field') or fn.child_by_field_name('method')
        if tbl is None or fld is None or tbl.type != 'identifier':
            return None
        key = (fp, f'{tbl.text.decode()}.{fld.text.decode()}')
        if key in self.returns:
            return self.returns[key]
        mod = self.aliases.get(fp, {}).get(tbl.text.decode())
        if mod and mod in self.mod2file:
            return ('@return', self.mod2file[mod], fld.text.decode())     # resolved later, after all files
        return None

    def recv_type(self, fp, line, recv):
        if recv.type == 'identifier':
            ty = self._lookup(fp, line, recv.text.decode())
        elif recv.type == 'dot_index_expression':
            base = recv.child_by_field_name('table')
            fld = recv.child_by_field_name('field')
            bt = self.recv_type(fp, line, base) if base is not None else None
            ty = self._field(bt, fld.text.decode()) if bt and fld is not None else None
        else:
            ty = None
        if isinstance(ty, tuple):                     # `local v = Mod.f()` with f's @return in Mod's file
            _, mfile, f = ty
            ty = next((r for (rf, k), r in self.returns.items() if rf == mfile and k.endswith('.' + f)), None)
        return ty

    def _field(self, cls, name, depth=0):
        while cls and depth < 8:
            if name in self.fields.get(cls, {}):
                return self.fields[cls][name]
            cls, depth = self.parent.get(cls), depth + 1
        return None

    def method(self, cls, name, depth=0):
        while cls and depth < 8:
            homes = self.class_files.get(cls, set())
            if len(homes) != 1:
                return None
            (fp, table), = homes
            q = f'{fp}::{table}.{name}'
            if q in self.node_names:
                return q
            cls, depth = self.parent.get(cls), depth + 1
        return None

    def bind(self, fp, line, name):
        """The one node a call `x:name()` / `x.name()` on this line means, by annotations, else None."""
        hits = set()
        for recv, m in self.calls.get((fp, line), []):
            if m != name:
                continue
            cls = self.recv_type(fp, line, recv)
            q = self.method(cls, name) if cls else None
            if q:
                hits.add((q, cls))
        return next(iter(hits)) if len(hits) == 1 else None
