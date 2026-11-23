"""Post-build pass that binds Java calls whose target is fixed by syntax (our fork; rule ``java.import-resolver``).

Stock CRG misses or leaves AMBIGUOUS cross-file Java calls such as (notebooks/15-java, gson):

- ``RuntimeTypeAdapterFactory.of(Shape.class, "type")``: three overloads named ``of``; CRG lists them all
- ``rta.registerSubtype(Circle.class)`` on a local typed ``RuntimeTypeAdapterFactory<Shape>``: overloads again
- ``new Gson()`` from another Maven module (extras/): the bare name ``Gson`` stays unresolved

What this pass does, on the stored graph, for .java files only:

1. Parse every file with tree-sitter (no text search) and index every type declaration by fully qualified name
   (``package.Outer.Inner``), with methods (name, parameter count, varargs, parameter type texts, static/final),
   constructors, fields and supertypes. A name declared in two files is dropped.
2. Resolve type names the way javac does: type parameters, member types, the file's own types, single-type
   imports, the same package, on-demand imports, java.lang; ``a.b.C`` written in full. A variable wins over a type.
3. Bind a call only when its target is certain from syntax: ``T.m()`` / ``a.b.T.m()`` (static, by argument count;
   overloads with the same count are skipped), ``import static`` calls, ``super.m()``, ``new T()`` (to the class
   node, as CRG already does for constructors), ``x.m()`` on a parameter/local/field whose declared type is a repo
   type when no repo subtype overrides m with that argument count, and inherited ``m()`` under the same
   no-override condition.
4. The CRG CALLS edge on that line (or the method name's line) with that call name is rewritten to the node, only
   when its target is not already a real node (or is a same-name duplicate ``C.m`` / ``C.m:S<hash>``). Its
   ``claimed_endpoint`` / ``ambiguous_*`` / ``collision_endpoint`` keys are dropped. Nothing is invented: a call
   with no CRG edge stays so, and a real node CRG chose is never overridden.

The parsing and classification code is the same as lib/java_callers_ref.py (the notebook's measuring stick), so
recall over the reference's *certain* calls is high by construction; the independent checks are the wrong-edge
count and the not-checkable edges, which must not change.

Iteration 2 (round 2), two more switches in this file, plus ``crg.keep-resolver-evidence`` in graph.py (CRG's
collision pass no longer turns an edge carrying ``collision_evidence`` back into AMBIGUOUS):

- ``java.single-impl``: ``x.m()`` on an interface with exactly one concrete repo implementation binds to that
  implementation's ``m`` (Spring service/DAO pattern). Closed world (this repo only); never for functional
  interfaces (< 2 abstract methods) or interfaces implemented by an anonymous class anywhere in the repo.
- ``java.fix-wrong``: corrects CRG's own wrong bindings, only where syntax is certain:
  (a) CRG bound the call to a different real node, and *every* call with that name on that line is certain
      (so the edge cannot be right for another call there): rewrite to the certain target. Seen: wrong overload by
      argument count, ``super.setUp()`` bound to the caller's own ``setUp``, an inherited call bound to a same-file
      overload with another arity.
  (b) every call with that name on the line is on a receiver whose declared type is external (JDK/library, not
      Object) and the bound class cannot be a subtype of it (e.g. ``list.addAll()`` on ``java.util.List`` bound
      to static ``Helpers.addAll``), or is a static call ``T.m()`` on an external type named by an import or
      java.lang: the edge goes back to the bare name, marked ``unresolved_targets: []`` (honest, never invented).
  Edges get ``rule = 'java.fix-wrong'`` and ``java_fixed_from`` (the node CRG had chosen).

Every rewritten edge gets ``extra.rule = 'java.import-resolver'`` and ``extra.java_via`` (static / new / typed /
static-import / inherited / super). Switch: ``CRG_RULES=off`` or ``CRG_RULES_OFF=java.import-resolver``; with
``CRG_RULES_ONLY`` set it runs only when named there.
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

RULE = 'java.import-resolver'
FIX_RULE = 'java.fix-wrong'            # Iteration 2: correct CRG's own wrong bindings
SINGLE_RULE = 'java.single-impl'        # Iteration 2: interface call with exactly one repo implementation


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


TYPE_DECL = ('class_declaration', 'interface_declaration', 'enum_declaration', 'record_declaration',
             'annotation_type_declaration')
CALLABLE = ('method_declaration', 'constructor_declaration', 'compact_constructor_declaration')
JAVA_LANG = set('''Object String Math System Integer Long Short Byte Double Float Boolean Character Number Class
Thread Runnable Iterable Comparable CharSequence StringBuilder StringBuffer Enum Record Exception RuntimeException
Error Throwable IllegalArgumentException IllegalStateException NullPointerException UnsupportedOperationException
AssertionError Void Override Deprecated SuppressWarnings FunctionalInterface SafeVarargs AutoCloseable Cloneable
ClassLoader Process ProcessBuilder Runtime StackTraceElement ThreadLocal InterruptedException
IndexOutOfBoundsException ArrayIndexOutOfBoundsException ClassCastException ArithmeticException
NumberFormatException CloneNotSupportedException ReflectiveOperationException ClassNotFoundException
NoSuchMethodException NoSuchFieldException SecurityException StrictMath Iterable Appendable Readable'''.split())


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _base_type(text):
    """'List<String>' -> 'List'; 'a.b.C<T>' -> 'a.b.C'; arrays and primitives -> None."""
    if not text:
        return None
    text = re.sub(r'@\w+(\([^)]*\))?\s*', '', text).strip()
    if text.endswith(']') or text.endswith('...'):
        return None
    text = re.sub(r'<.*>', '', text).strip()
    if not re.fullmatch(r'[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*', text):
        return None
    if text in ('int', 'long', 'short', 'byte', 'char', 'boolean', 'double', 'float', 'void', 'var'):
        return None
    return text


def _params(n):
    """(count, varargs, [param type texts])"""
    ps = n.child_by_field_name('parameters')
    if ps is None:
        return 0, False, []
    items = [c for c in ps.named_children if c.type in ('formal_parameter', 'spread_parameter', 'receiver_parameter')]
    items = [c for c in items if c.type != 'receiver_parameter']
    texts = []
    for c in items:
        ty = c.child_by_field_name('type') or next((x for x in c.named_children if x.type != 'modifiers'), None)
        texts.append(re.sub(r'\s+', '', re.sub(r'<.*>', '', _t(ty))))
    return len(items), bool(items) and items[-1].type == 'spread_parameter', texts


def _mods(n):
    m = next((c for c in n.children if c.type == 'modifiers'), None)
    return _t(m) if m is not None else ''


def parse_file(path):
    src = Path(path).read_bytes()
    tree = get_parser('java').parse(src)
    out = {'package': '', 'imports': {}, 'static_imports': {}, 'static_wild': [], 'wild': [], 'types': {},
           'calls': [], 'path': path, 'anon_types': []}
    types = out['types']                         # 'Outer.Inner' -> record

    def new_type(n, tpath, kind):
        sup = []
        sc = n.child_by_field_name('superclass')
        if sc is not None:
            sup += [_t(c) for c in sc.named_children]
        for fld in ('interfaces',):
            si = n.child_by_field_name(fld)
            if si is not None:
                for tl in si.named_children:
                    sup += [_t(c) for c in (tl.named_children if tl.type == 'type_list' else [tl])]
        ei = next((c for c in n.children if c.type == 'extends_interfaces'), None)
        if ei is not None:
            for tl in ei.named_children:
                sup += [_t(c) for c in (tl.named_children if tl.type == 'type_list' else [tl])]
        tparams = set()
        tp = n.child_by_field_name('type_parameters') or next((c for c in n.children if c.type == 'type_parameters'), None)
        if tp is not None:
            for p in tp.named_children:
                ident = next((c for c in p.named_children if c.type in ('type_identifier', 'identifier')), None)
                if ident is not None:
                    tparams.add(_t(ident))
        rec = {'path': tpath, 'kind': kind, 'line': n.start_point[0] + 1, 'supers': [s for s in sup if s],
               'methods': [], 'ctors': [], 'fields': {}, 'tparams': tparams, 'members': set(), 'final': 'final' in _mods(n),
               'abstract': 'abstract' in _mods(n)}
        if kind == 'record_declaration':
            cnt, va, texts = _params(n)
            rec['record_arity'] = cnt
        types[tpath] = rec
        return rec

    def collect_scope(body, scope):
        """Declared names -> type text (None = untyped) inside a callable body, not entering nested types."""
        stack = [body]
        while stack:
            x = stack.pop()
            t = x.type
            if t in TYPE_DECL or t == 'class_body':
                continue
            if t in ('local_variable_declaration',):
                ty = _t(x.child_by_field_name('type'))
                for d in x.named_children:
                    if d.type == 'variable_declarator':
                        scope.setdefault(_t(d.child_by_field_name('name')), set()).add(ty)
            elif t in ('formal_parameter', 'spread_parameter'):
                ty = _t(x.child_by_field_name('type') or next((c for c in x.named_children if c.type != 'modifiers'), None))
                nm = x.child_by_field_name('name')
                if nm is None:
                    vd = next((c for c in x.named_children if c.type == 'variable_declarator'), None)
                    nm = vd.child_by_field_name('name') if vd is not None else None
                if nm is not None:
                    scope.setdefault(_t(nm), set()).add(ty + ('[]' if t == 'spread_parameter' else ''))
            elif t == 'enhanced_for_statement':
                scope.setdefault(_t(x.child_by_field_name('name')), set()).add(_t(x.child_by_field_name('type')))
            elif t == 'catch_formal_parameter':
                ct = next((c for c in x.named_children if c.type == 'catch_type'), None)
                scope.setdefault(_t(x.child_by_field_name('name')), set()).add(_t(ct) if ct is not None and ct.named_child_count == 1 else '?')
            elif t == 'instanceof_expression' and x.child_by_field_name('name') is not None:
                scope.setdefault(_t(x.child_by_field_name('name')), set()).add(_t(x.named_children[1]))
            elif t == 'lambda_expression':
                ps = x.child_by_field_name('parameters')
                if ps is not None:
                    if ps.type == 'identifier':
                        scope.setdefault(_t(ps), set()).add(None)
                    else:
                        for p in ps.named_children:
                            if p.type == 'identifier':
                                scope.setdefault(_t(p), set()).add(None)
            elif t == 'resource' and x.child_by_field_name('name') is not None:
                scope.setdefault(_t(x.child_by_field_name('name')), set()).add(_t(x.child_by_field_name('type')))
            stack.extend(x.children)

    def walk(n, ctx, scope, mtparams, in_anon):
        """ctx: list of enclosing type paths (innermost last)."""
        t = n.type
        if t == 'package_declaration':
            out['package'] = _t(next(c for c in n.named_children if c.type in ('scoped_identifier', 'identifier')))
            return
        if t == 'import_declaration':
            is_static = any(c.type == 'static' for c in n.children)
            name = _t(next(c for c in n.named_children if c.type in ('scoped_identifier', 'identifier')))
            wild = any(c.type == 'asterisk' for c in n.named_children)
            if is_static and wild:
                out['static_wild'].append(name)
            elif is_static:
                cls, m = name.rsplit('.', 1)
                out['static_imports'].setdefault(m, set()).add(cls)
            elif wild:
                out['wild'].append(name)
            else:
                out['imports'][name.rsplit('.', 1)[-1]] = name
            return
        if t in TYPE_DECL:
            nm = _t(n.child_by_field_name('name'))
            tpath = (ctx[-1] + '.' + nm) if ctx else nm
            if ctx:
                types[ctx[-1]]['members'].add(nm)
            rec = new_type(n, tpath, t)
            body = n.child_by_field_name('body')
            if body is not None:
                for c in body.children:
                    walk(c, ctx + [tpath], {}, set(), False)
            return
        if t in ('class_body', 'enum_body', 'enum_body_declarations', 'interface_body', 'annotation_type_body'):
            for c in n.children:
                walk(c, ctx, scope, mtparams, in_anon)
            return
        if t == 'enum_constant':
            args = n.child_by_field_name('arguments')
            if args is not None:
                walk(args, ctx, scope, mtparams, in_anon)
            body = n.child_by_field_name('body')
            if body is not None:
                for c in body.children:
                    walk(c, ctx, {}, set(), True)
            return
        if t == 'field_declaration' and ctx and not in_anon:
            ty = _t(n.child_by_field_name('type'))
            for d in n.named_children:
                if d.type == 'variable_declarator':
                    types[ctx[-1]]['fields'][_t(d.child_by_field_name('name'))] = ty
        if t in CALLABLE and ctx:
            cnt, va, texts = _params(n)
            mods = _mods(n)
            rec = types[ctx[-1]]
            entry = {'line': n.start_point[0] + 1, 'nparams': cnt, 'varargs': va, 'ptypes': texts,
                     'static': 'static' in mods, 'final': 'final' in mods or 'private' in mods,
                     'abstract': n.child_by_field_name('body') is None, 'name': _t(n.child_by_field_name('name'))}
            if not in_anon:
                if t == 'method_declaration':
                    rec['methods'].append(entry)
                else:
                    if t == 'compact_constructor_declaration':
                        entry['nparams'] = rec.get('record_arity', 0)
                    rec['ctors'].append(entry)
            sc = {}
            collect_scope(n, sc)
            tps = set(mtparams)
            tp = next((c for c in n.children if c.type == 'type_parameters'), None)
            if tp is not None:
                tps |= {_t(next((c for c in p.named_children), p)) for p in tp.named_children}
            merged = {**scope, **sc} if in_anon else sc
            for c in n.children:
                walk(c, ctx, merged, tps, in_anon)
            return
        if t == 'object_creation_expression':
            ty = n.child_by_field_name('type')
            args = n.child_by_field_name('arguments')
            body = next((c for c in n.children if c.type == 'class_body'), None)
            if ty is not None and args is not None and ctx:
                out['calls'].append({'line': n.start_point[0] + 1, 'pline': ty.start_point[0] + 1, 'kind': 'new',
                                     'type': _t(ty), 'name': (_base_type(_t(ty)) or '?').rsplit('.', 1)[-1],
                                     'nargs': args.named_child_count, 'ctx': list(ctx), 'scope': scope,
                                     'mtp': mtparams, 'anon': in_anon})
            if body is not None and ty is not None and ctx:      # `new I() { ... }`: an implementation of I
                out['anon_types'].append((_t(ty), list(ctx)))
            for c in n.children:
                if c is body:
                    for cc in body.children:
                        walk(cc, ctx, scope, mtparams, True)
                else:
                    walk(c, ctx, scope, mtparams, in_anon)
            return
        if t == 'method_invocation' and ctx:
            obj, nm, args = n.child_by_field_name('object'), n.child_by_field_name('name'), n.child_by_field_name('arguments')
            if nm is not None and args is not None:
                c = {'line': n.start_point[0] + 1, 'pline': nm.start_point[0] + 1, 'name': _t(nm),
                     'nargs': args.named_child_count, 'ctx': list(ctx), 'scope': scope, 'mtp': mtparams, 'anon': in_anon}
                if obj is None:
                    c['kind'] = 'bare'
                elif obj.type == 'super':
                    c['kind'] = 'super'
                elif obj.type == 'this':
                    c['kind'] = 'this'
                elif obj.type == 'identifier':
                    c['kind'], c['recv'] = 'ident', _t(obj)
                elif obj.type == 'field_access':
                    o2 = obj.child_by_field_name('object')
                    if o2 is not None and o2.type == 'this':
                        c['kind'], c['recv'] = 'thisfield', _t(obj.child_by_field_name('field'))
                    else:
                        c['kind'], c['recv'] = 'dotted', _t(obj)
                else:
                    c['kind'] = 'other'
                out['calls'].append(c)
        for c in n.children:
            walk(c, ctx, scope, mtparams, in_anon)

    walk(tree.root_node, [], {}, set(), False)
    return out


class Index:
    """All parsed files: types by fully qualified name, name resolution, method lookup, subtypes."""

    def __init__(self, files):
        self.P = {}
        for f in files:
            try:
                self.P[f] = parse_file(f)
            except (OSError, ValueError):
                pass
        fq = defaultdict(list)
        for f, p in self.P.items():
            for tpath in p['types']:
                fq[(p['package'] + '.' if p['package'] else '') + tpath].append((f, tpath))
        # A fully qualified name declared in several files (guava ships `guava/` and `android/guava/` copies) keeps
        # every copy under its own key `fqn#i`; pick() chooses the copy for a given caller.
        self.copies, self.types, self.plain = {}, {}, {}
        for k, v in fq.items():
            keys = [k] if len(v) == 1 else [f'{k}#{i}' for i in range(len(v))]
            self.copies[k] = keys
            for key, fv in zip(keys, v):
                self.types[key] = fv
                self.plain[key] = k
        self.dup = {k for k, v in fq.items() if len(v) > 1}
        self.fqn_of = {v: k for k, v in self.types.items()}
        self.near = False
        self._sup = {}
        self.children = defaultdict(set)
        for k in self.types:
            for s in self.supers(k)[0]:
                self.children[s].add(k)

    def rec(self, fqn):
        f, tpath = self.types[fqn]
        return self.P[f]['types'][tpath]

    def has(self, fqn):
        return fqn in self.copies

    def pick(self, fqn, near_file):
        """The copy of fqn a caller in near_file compiles against: the only one; the one in the same file; else
        the copy whose path shares the longest prefix with the caller (same source tree / flavor). A tie -> None.
        Sets self.near when the choice was by path."""
        keys = self.copies.get(fqn)
        if not keys:
            return None
        if len(keys) == 1:
            return keys[0]
        same = [k for k in keys if self.types[k][0] == near_file]
        if len(same) == 1:
            return same[0]

        def lcp(a, b):
            n = 0
            for x, y in zip(a, b):
                if x != y:
                    break
                n += 1
            return n
        scored = sorted(((lcp(self.types[k][0], near_file), k) for k in keys), reverse=True)
        if scored[0][0] == scored[1][0]:
            return None
        self.near = True
        return scored[0][1]

    def resolve(self, f, ctx, text, mtp=()):
        """Type text in file f, inside enclosing type paths ctx -> ('repo', key) | ('ext', name) | (None, None)."""
        base = _base_type(text)
        if base is None:
            return None, None
        p = self.P[f]
        pkg = p['package']
        parts = base.split('.')
        head = parts[0]
        found = None
        tps = set(mtp)
        for tpath in ctx:
            tps |= p['types'][tpath]['tparams'] if tpath in p['types'] else set()
        if head in tps and len(parts) == 1:
            return None, None
        for tpath in reversed(ctx):                        # member types of enclosing types (and their repo supers)
            for anc in self._self_and_ancestors(self.fqn_of.get((f, tpath))):
                r = self.rec(anc)
                if head in r['members']:
                    found = self.pick(self.plain[anc] + '.' + head, self.types[anc][0])
                    break
            if found:
                break
            if tpath.rsplit('.', 1)[-1] == head:
                found = self.fqn_of.get((f, tpath))
                break
        if not found and head in p['types']:
            found = self.fqn_of.get((f, head))
        if not found and head in p['imports']:
            imp = p['imports'][head]
            if self.has(imp):
                found = self.pick(imp, f)
                if found is None:
                    return None, None
            else:
                return ('ext', imp) if len(parts) == 1 else (None, None)
        if not found:
            cand = (pkg + '.' if pkg else '') + head
            if self.has(cand):
                found = self.pick(cand, f)
                if found is None:
                    return None, None
        if not found:
            hits = [w + '.' + head for w in p['wild'] if self.has(w + '.' + head)]
            if len(hits) == 1:
                found = self.pick(hits[0], f)
                if found is None:
                    return None, None
            elif len(hits) > 1:
                return None, None
        if not found and len(parts) > 1:                   # fully qualified a.b.C(.Inner)
            for i in range(len(parts), 0, -1):
                cand = '.'.join(parts[:i])
                if self.has(cand):
                    found, parts = self.pick(cand, f), [cand] + parts[i:]
                    if found is None:
                        return None, None
                    break
        if not found:
            if head in JAVA_LANG and len(parts) == 1:
                return 'ext', 'java.lang.' + head
            if head[:1].isupper() and len(parts) == 1:
                return 'ext', head
            return None, None
        for seg in parts[1:]:
            nxt = self.pick(self.plain[found] + '.' + seg, self.types[found][0])
            if nxt is None:
                return None, None
            found = nxt
        return 'repo', found

    def supers(self, fqn):
        """(repo supertypes, has an external/unresolved supertype)"""
        if fqn in self._sup:
            return self._sup[fqn]
        self._sup[fqn] = ([], False)
        f, tpath = self.types[fqn]
        r = self.rec(fqn)
        outer = tpath.split('.')
        ctx = ['.'.join(outer[:i]) for i in range(1, len(outer))]
        reps, ext = [], False
        for s in r['supers']:
            kind, v = self.resolve(f, ctx, s)
            if kind == 'repo' and v != fqn:
                reps.append(v)
            else:
                ext = True
        self._sup[fqn] = (reps, ext)
        return self._sup[fqn]

    def _self_and_ancestors(self, fqn):
        if fqn is None or fqn not in self.types:
            return []
        if not hasattr(self, '_anc'):
            self._anc = {}
        if fqn in self._anc and len(self._sup) == self._anc[fqn][1]:
            return self._anc[fqn][0]
        seen, order, q = set(), [], [fqn]
        while q:
            x = q.pop(0)
            if x in seen or x not in self.types:
                continue
            seen.add(x)
            order.append(x)
            q.extend(self.supers(x)[0])
        self._anc[fqn] = (order, len(self._sup))
        return order

    def chain_has_external(self, fqn):
        return any(self.supers(a)[1] for a in self._self_and_ancestors(fqn))

    def descendants(self, fqn):
        if not hasattr(self, '_desc'):
            self._desc = {}
        if fqn in self._desc:
            return self._desc[fqn]
        seen, q = set(), [fqn]
        while q:
            x = q.pop()
            for c in self.children.get(x, ()):
                if c not in seen:
                    seen.add(c)
                    q.append(c)
        self._desc[fqn] = seen
        return seen

    @staticmethod
    def _arity_ok(m, n):
        return m['nparams'] == n or (m['varargs'] and n >= m['nparams'] - 1)

    def lookup(self, fqn, name, nargs):
        """Unique method `name` with nargs args in fqn or its repo ancestors -> (declaring fqn, entry) or None.
        'none' when no method of that name exists at all in the repo chain."""
        sigs, any_name = {}, False
        for anc in self._self_and_ancestors(fqn):
            for m in self.rec(anc)['methods']:
                if m['name'] != name:
                    continue
                any_name = True
                if self._arity_ok(m, nargs):
                    sigs.setdefault(tuple(m['ptypes']), (anc, m))      # nearest declaration of a signature wins
        if not any_name:
            return 'none'
        if len(sigs) != 1:
            return None
        return next(iter(sigs.values()))

    def anon_impl(self):
        """Repo types implemented somewhere by an anonymous class (`new I() { ... }`)."""
        if not hasattr(self, '_anon'):
            self._anon = set()
            for f, p in self.P.items():
                for text, ctx in p.get('anon_types', ()):
                    kind, v = self.resolve(f, ctx, text)
                    if kind == 'repo':
                        self._anon.add(v)
        return self._anon

    def single_impl(self, fqn, name, nargs):
        """Interface fqn with exactly one concrete repo implementation (the Spring service/DAO pattern):
        -> (declaring fqn, method entry) of the method that runs, else None. Closed world: only this repo.
        Not for functional interfaces (<2 abstract methods: lambdas may implement them, invisible here) or
        interfaces implemented by an anonymous class anywhere in the repo."""
        r = self.rec(fqn)
        if r['kind'] != 'interface_declaration':
            return None
        if sum(m['abstract'] and not m['static'] for m in r['methods']) < 2:
            return None
        desc = self.descendants(fqn)
        anon = self.anon_impl()
        if fqn in anon or desc & anon:
            return None
        conc = [d for d in desc if self.rec(d)['kind'] in ('class_declaration', 'enum_declaration', 'record_declaration')
                and not self.rec(d).get('abstract')]
        if len(conc) != 1:
            return None
        got = self.lookup(conc[0], name, nargs)
        if got in (None, 'none') or got[1]['abstract'] or got[0] == fqn:
            return None
        return got

    def overridden(self, fqn, name, nargs):
        return any(m['name'] == name and self._arity_ok(m, nargs)
                   for d in self.descendants(fqn) for m in self.rec(d)['methods'])

    def ctor(self, fqn, nargs):
        r = self.rec(fqn)
        if r['kind'] in ('interface_declaration', 'annotation_type_declaration'):
            return None
        cs = [c for c in r['ctors'] if self._arity_ok(c, nargs)]
        if not r['ctors']:
            if r['kind'] == 'record_declaration':
                return ('class', None) if nargs == r.get('record_arity', 0) else None
            return ('class', None) if nargs == 0 else None
        if len(cs) == 1:
            return ('ctor', cs[0])
        return None


def _var_type(c, idx, f):
    """Declared type text of an identifier receiver: scope (locals/params), then fields of enclosing types."""
    v = c['recv']
    if v in c['scope']:
        ts = c['scope'][v]
        return ('typed', next(iter(ts))) if len(ts) == 1 and None not in ts else ('untyped', None)
    p = idx.P[f]
    for tpath in reversed(c['ctx']):
        for anc in idx._self_and_ancestors(idx.fqn_of.get((f, tpath))):
            fl = idx.rec(anc)['fields']
            if v in fl:
                return 'typed', fl[v] if anc == idx.fqn_of.get((f, tpath)) else ('@' + anc, fl[v])
    return None, None


def classify(idx, f, c):
    """-> dict with 'class' in certain / virtual / external / same-file / unknown, and 'target' for certain.
    'near' is set on a certain call whose target needed pick() to choose between duplicate copies of a class."""
    idx.near = False
    r = _classify(idx, f, c)
    if r['class'] == 'certain' and idx.near:
        r['near'] = True
    return r


def _classify(idx, f, c):
    k = c['kind']
    ctx = c['ctx']
    here = idx.fqn_of.get((f, ctx[-1]))
    res = {'class': 'unknown'}

    def at(decl_fqn, entry, kind='method'):
        tf, tpath = idx.types[decl_fqn]
        if tf == f:
            return {'class': 'same-file'}
        lines = {entry['line']} if entry else set()
        lines.add(idx.rec(decl_fqn)['line'])
        if kind == 'method':
            lines = {entry['line']}
        return {'class': 'certain', 'target_file': tf, 'target_lines': lines, 'decl': decl_fqn}

    def method_on_type(fqn, virtual_check):
        got = idx.lookup(fqn, c['name'], c['nargs'])
        if got in (None, 'none'):
            return {'class': 'unknown'}
        decl, m = got
        if virtual_check and not (m['static'] or m['final'] or idx.rec(decl)['final']) \
                and idx.overridden(fqn, c['name'], c['nargs']):
            one = idx.single_impl(fqn, c['name'], c['nargs'])
            if one is None:
                return {'class': 'virtual'}
            r = at(*one)
            if r['class'] == 'certain':
                r['single'] = True                       # interface call, exactly one repo implementation
            return r
        return at(decl, m)

    if k == 'new':
        if c['anon'] and False:
            return res
        kind, v = idx.resolve(f, ctx, c['type'], c['mtp'])
        if kind != 'repo':
            return {'class': 'external' if kind == 'ext' else 'unknown'}
        got = idx.ctor(v, c['nargs'])
        if got is None:
            return res
        tf = idx.types[v][0]
        if tf == f:
            return {'class': 'same-file'}
        lines = {idx.rec(v)['line']}
        if got[0] == 'ctor':
            lines.add(got[1]['line'])
        return {'class': 'certain', 'target_file': tf, 'target_lines': lines, 'decl': v}
    if c['anon'] and k in ('bare', 'this', 'super'):
        return res
    if k in ('bare', 'this'):
        # nearest enclosing type whose repo chain declares the name; external supers make it unknown
        for tpath in reversed(ctx):
            fq = idx.fqn_of.get((f, tpath))
            if fq is None:
                return res
            got = idx.lookup(fq, c['name'], c['nargs'])
            if got == 'none':
                if idx.chain_has_external(fq):
                    return res                          # an external supertype may declare it
                if k == 'this':
                    return res
                continue
            if got is None:
                return res
            decl, m = got
            if decl == fq or idx.types[decl][0] == f:
                return {'class': 'same-file'}
            if not (m['static'] or m['final']) and idx.overridden(fq, c['name'], c['nargs']):
                return {'class': 'virtual'}
            r = at(decl, m)
            if r['class'] == 'certain':
                r['via'] = 'inherited'
            return r
        if k == 'bare':                                 # static imports
            p = idx.P[f]
            owners = {idx.pick(o, f) for o in p['static_imports'].get(c['name'], ()) if idx.has(o)}
            owners |= {k for k in (idx.pick(w, f) for w in p['static_wild'] if idx.has(w))
                       if k and any(m['name'] == c['name'] for m in idx.rec(k)['methods'])}
            owners.discard(None)
            if len(owners) == 1:
                got = idx.lookup(next(iter(owners)), c['name'], c['nargs'])
                if got not in (None, 'none'):
                    r = at(*got)
                    if r['class'] == 'certain':
                        r['via'] = 'static-import'
                    return r
        return res
    if k == 'super':
        fq = idx.fqn_of.get((f, ctx[-1]))
        sups = [s for s in idx.supers(fq)[0] if idx.rec(s)['kind'] != 'interface_declaration'] if fq else []
        if len(sups) != 1:
            return res
        got = idx.lookup(sups[0], c['name'], c['nargs'])
        if got in (None, 'none'):
            return res
        r = at(*got)
        if r['class'] == 'certain':
            r['via'] = 'super'
        return r
    if k in ('ident', 'thisfield'):
        if k == 'ident':
            vk, vt = _var_type(c, idx, f)
        else:
            vk, vt = None, None
            for tpath in reversed(ctx):
                fl = idx.P[f]['types'][tpath]['fields']
                if c['recv'] in fl:
                    vk, vt = 'typed', fl[c['recv']]
                    break
        if vk == 'untyped':
            return res
        if vk is None and k == 'ident':                 # not a variable: maybe a type -> static call
            kind, v = idx.resolve(f, ctx, c['recv'], c['mtp'])
            if kind == 'repo':
                r = method_on_type(v, False)
                if r['class'] == 'certain':
                    r['via'] = 'static'
                return r
            if kind == 'ext':
                return {'class': 'external-static', 'ext_type': v}
            return res
        if vk is None:
            return res
        tctx, rf = ctx, f
        if isinstance(vt, tuple):                        # field inherited from a repo supertype: resolve there
            anc, vt = vt
            rf, atp = idx.types[anc[1:]]
            outer = atp.split('.')
            tctx = ['.'.join(outer[:i]) for i in range(1, len(outer) + 1)]
        kind, v = idx.resolve(rf, tctx, vt, c['mtp'] if rf == f else ())
        if kind == 'repo':
            r = method_on_type(v, True)
            if r['class'] == 'certain':
                r['via'] = 'typed'
            return r
        if kind == 'ext':
            return {'class': 'external', 'ext_type': v}
        return res
    if k == 'dotted':
        kind, v = idx.resolve(f, ctx, c['recv'], c['mtp'])
        if kind == 'repo' and '.' in c['recv']:
            r = method_on_type(v, False)
            if r['class'] == 'certain':
                r['via'] = 'static'
            return r
        return res
    return res


def _base(qn):
    return re.sub(r':S[0-9a-f]+$', '', qn)


def _short(target):
    return _base(target.rsplit('::', 1)[-1]).rsplit('.', 1)[-1]


def _switch(name):
    try:
        from .rules import switch_on
    except ImportError:  # pragma: no cover - older fork without the helper
        return enabled()
    return switch_on(name)


def _cannot_be(idx, target_qn, ext_names):
    """True when the class owning node target_qn can't be a subtype of any of ext_names (external type names)."""
    if '::' not in target_qn:
        return False
    fp, tail = target_qn.split('::', 1)
    tail = _base(tail)
    owner = None
    parts = tail.split('.')
    for i in range(len(parts), 0, -1):
        fq = idx.fqn_of.get((fp, '.'.join(parts[:i])))
        if fq:
            owner = fq
            break
    if owner is None:
        return False
    chain = idx._self_and_ancestors(owner)
    if any(idx.supers(a)[1] for a in chain):
        return False
    names = {idx.plain[a].rsplit('.', 1)[-1] for a in chain}
    return not (names & ext_names)


def resolve(store: GraphStore) -> dict:
    stats = {'enabled': enabled(), 'fix_wrong': _switch(FIX_RULE), 'single_impl': _switch(SINGLE_RULE),
             'files': 0, 'certain_calls': 0, 'calls_resolved': 0, 'already_bound': 0,
             'no_crg_edge': 0, 'no_target_node': 0, 'crg_bound_elsewhere': 0,
             'fixed_wrong_to_certain': 0, 'fixed_wrong_to_unresolved': 0, 'single_impl_bound': 0}
    if not stats['enabled']:
        return stats
    fix_wrong, single_on = stats['fix_wrong'], stats['single_impl']
    files = [f for f in store.get_all_files() if f.lower().endswith('.java')]
    if not files:
        return stats
    idx = Index(files)
    stats['files'] = len(idx.P)
    conn = store._conn  # post-build maintenance pass, like the other resolvers
    fileset = set(files)
    node_at = defaultdict(list)
    for row in conn.execute("SELECT name, qualified_name, file_path, line_start, kind FROM nodes "
                            "WHERE kind IN ('Function','Class','Test')"):
        if row['file_path'] in fileset:
            node_at[(row['file_path'], row['line_start'])].append((row['name'], row['qualified_name'], row['kind']))
    node_names = {r[0] for r in conn.execute('SELECT qualified_name FROM nodes')}
    edges_at = defaultdict(list)
    for row in conn.execute("SELECT id, target_qualified, file_path, line, extra FROM edges WHERE kind='CALLS'"):
        if row['file_path'] in fileset:
            edges_at[(row['file_path'], row['line'])].append((row['id'], row['target_qualified'], row['extra']))

    # classify every call once; per line (first line and method-name line) keep what each call with a name is
    classified = []
    by_line = defaultdict(list)
    for f, p in idx.P.items():
        for c in p['calls']:
            r = classify(idx, f, c)
            if r.get('single') and not single_on:
                r = {'class': 'virtual'}               # round-1 view: an interface call is virtual
            classified.append((f, c, r))
            for ln in {c['line'], c['pline']}:
                by_line[(f, ln, c['name'])].append(r)

    def tag(extra, rule, **kw):
        d = json.loads(extra or '{}') or {}
        for k in ('claimed_endpoint', 'ambiguous_targets', 'ambiguous_target_count',
                  'ambiguous_targets_truncated', 'unresolved_targets', 'collision_endpoint'):
            d.pop(k, None)
        return {**d, 'rule': rule, 'collision_evidence': rule, **kw}

    updates, done = [], set()
    for f, c, r in classified:
        if r['class'] != 'certain':
            continue
        stats['certain_calls'] += 1
        tf = r['target_file']
        if c['kind'] == 'new':                         # CRG's convention: `new T()` -> the class node
            cl = idx.rec(r['decl'])['line']
            nodes = [nd for nd in node_at.get((tf, cl), []) if nd[2] == 'Class']
        else:
            nodes = [nd for ln in r['target_lines'] for nd in node_at.get((tf, ln), [])
                     if nd[2] in ('Function', 'Test')]
            if len(nodes) > 1:
                nodes = [nd for nd in nodes if nd[0] == c['name']] or nodes
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
        # Iteration 2 (java.fix-wrong): CRG's real-node choice may be overridden only when every call with this
        # name on these lines is certain, so the edge cannot be right for some other call written there
        here = by_line[(f, c['line'], c['name'])] + (by_line[(f, c['pline'], c['name'])] if c['pline'] != c['line'] else [])
        all_certain = all(x['class'] == 'certain' for x in here)
        bound = False
        for eid, tq, extra in cands:
            wrong_node = tq in node_names and _base(tq) != _base(target)
            if wrong_node and not (fix_wrong and all_certain):
                continue                               # CRG chose another real node; not ours to override
            if wrong_node:
                rule, kw = FIX_RULE, {'java_fixed_from': tq.rsplit('::', 1)[-1]}
                stats['fixed_wrong_to_certain'] += 1
            else:
                rule, kw = RULE, {}
            kw['java_via'] = 'single-impl' if r.get('single') else r.get('via', c['kind'])
            if r.get('single'):
                stats['single_impl_bound'] += 1
            updates.append((target, json.dumps(tag(extra, rule, **kw)), eid))
            done.add(eid)
            bound = True
            break
        if not bound:
            stats['crg_bound_elsewhere'] += 1

    # Iteration 2 (java.fix-wrong), external receivers: CRG bound to a repo node a call that syntax sends outside
    # the repo. `list.addAll(x)` with `List<T> list` (java.util.List) cannot run a repo method unless that method's
    # class is a subtype of List; `Objects.equals(a, b)` (static, external type) can never reach a repo node. The
    # edge goes back to the bare name, marked checked-unresolved (`unresolved_targets: []`), never invented.
    ext_updates = []
    if fix_wrong:
        for f, c, r in classified:
            if r['class'] not in ('external', 'external-static'):
                continue
            here = by_line[(f, c['line'], c['name'])] + (by_line[(f, c['pline'], c['name'])] if c['pline'] != c['line'] else [])
            for eid, tq, extra in [e for ln in dict.fromkeys((c['line'], c['pline'])) for e in edges_at.get((f, ln), [])
                                   if _short(e[1]) == c['name'] and e[0] not in done]:
                if tq not in node_names:
                    continue
                if all(x['class'] == 'external' and x.get('ext_type', '').rsplit('.', 1)[-1] != 'Object' for x in here):
                    ok = _cannot_be(idx, tq, {x.get('ext_type', '').rsplit('.', 1)[-1] for x in here})
                elif all(x['class'] == 'external-static' and '.' in x.get('ext_type', '') for x in here):
                    ok = True
                else:
                    ok = False
                if not ok:
                    continue
                d = tag(extra, FIX_RULE, java_fixed_from=tq.rsplit('::', 1)[-1], unresolved_targets=[],
                        java_external=r.get('ext_type', ''))
                ext_updates.append((c['name'], json.dumps(d), eid))
                done.add(eid)
                stats['fixed_wrong_to_unresolved'] += 1
    if updates or ext_updates:
        store._begin_immediate()
        conn.executemany('UPDATE edges SET target_qualified = ?, extra = ? WHERE id = ?', updates + ext_updates)
        store.commit()
        store._invalidate_cache()
    stats['calls_resolved'] = len(updates)
    logger.info('Java import resolution: %s', stats)
    return stats
