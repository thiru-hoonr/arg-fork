"""Shared tree-sitter walker for C / C++ / Objective-C module classes and edges (same row contract and conventions as
edgeref_c.py). Source is run through core-module/extract.py c_prep / cpp_prep / objc_prep first (macros blanked, lines kept)."""
import re, sys
from pathlib import Path
from tree_sitter_language_pack import get_parser
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core-module'))
import extract

INC = re.compile(r'^\s*#\s*(include|import)\b')
CLASS = {'struct_specifier', 'union_specifier', 'enum_specifier', 'class_specifier'}
SKIP = {'preproc_def', 'preproc_function_def', 'comment', 'string_literal'}
NAMEY = ('identifier', 'field_identifier', 'type_identifier', 'destructor_name', 'operator_name')


STD_MACROS = {'assert', 'va_start', 'va_end', 'va_arg', 'va_copy', 'offsetof', 'static_assert', 'isnan', 'isinf', 'isfinite', 'signbit', 'fpclassify', 'setjmp'}


PREDEF = {'__APPLE__', '__MACH__', '__STDC__', '__GNUC__', '__clang__', '__LP64__', '__x86_64__', '__aarch64__', '__arm64__', '__STDC_VERSION__', '__unix__', '__ORDER_LITTLE_ENDIAN__', '__BYTE_ORDER__'}
_TOK = re.compile(r'^\s*(!?)\s*(?:defined\s*\(?\s*(\w+)\s*\)?|(\d+)|(\w+))\s*$')


def _cond(n, defs):
    """True / False for `#ifdef X`, `#ifndef X`, `#if [!]defined(X)`, `#if 0|1`, `#if X` (undefined X is 0); None otherwise."""
    if n.type in ('preproc_ifdef', 'preproc_elifdef'):
        nm = n.child_by_field_name('name')
        if nm is None: return None
        neg = any(c.type in ('#ifndef', '#elifndef') for c in n.children)
        return (nm.text.decode() in defs) != neg
    c = n.child_by_field_name('condition')
    m = _TOK.match(c.text.decode()) if c is not None else None
    if not m: return None
    neg, d, num, w = m.groups()
    if d: v = d in defs
    elif num: v = int(num) != 0
    elif w in defs: return None      # value unknown
    else: v = False
    return v != bool(neg)


def _macro(name, macros, types):
    return name in macros or name in types


def _callee(n):
    while n is not None:
        t = n.type
        if t in NAMEY: return n
        if t == 'field_expression': n = n.child_by_field_name('field')
        elif t in ('qualified_identifier', 'template_function', 'template_method'): n = n.child_by_field_name('name')
        elif t == 'parenthesized_expression': n = n.named_children[0] if n.named_children else None
        else: return None
    return None


def _first_kw(n):
    for c in n.children:
        if c.type in ('identifier', 'field_identifier') and n.child_by_field_name('receiver') != c:
            return c
    return None


def edges_for(root, files, lang, prep):
    p = get_parser(lang); out = []
    # macro / typedef names visible to a file: its own + those of repo headers it #includes (transitively, by basename),
    # like the preprocessor. A function-like macro or a typedef in callee position is no call (clang sees the expansion / a cast).
    MAC, TYP, INCS, DEF, base = {}, {}, {}, {}, {}
    for rel in files:
        b = (Path(root) / rel).read_bytes()
        DEF[rel] = {m.decode() for m in re.findall(rb'(?m)^\s*#\s*define\s+(\w+)', b)}
        MAC[rel] = {m.decode() for m in re.findall(rb'(?m)^\s*#\s*define\s+(\w+)\(', b)}
        TYP[rel] = {m.decode() for m in re.findall(rb'\btypedef\b[^;{]*?\b(\w+)\s*;', b)}
        INCS[rel] = [m.decode() for m in re.findall(rb'(?m)^\s*#\s*(?:include|import)\s*"([^"]+)"', b)]
        base.setdefault(Path(rel).name, rel)
    def visible_files(rel):
        seen, todo = set(), [rel]
        while todo:
            r = todo.pop()
            if r in seen: continue
            seen.add(r); todo += [base[Path(i).name] for i in INCS.get(r, []) if Path(i).name in base]
        return seen
    def visible(rel):
        seen, todo = set(), [rel]
        while todo:
            r = todo.pop()
            if r in seen: continue
            seen.add(r); todo += [base[Path(i).name] for i in INCS.get(r, []) if Path(i).name in base]
        return set().union(*(MAC[r] for r in seen)) | STD_MACROS, set().union(*(TYP[r] for r in seen)), set().union(*(DEF[r] for r in seen)) | PREDEF
    for rel in files:
        raw = (Path(root) / rel).read_bytes()
        macros, types, defs = visible(rel)
        own = {}   # this file's own #defines count only below their line (include guards: `#ifndef X_H` / `#define X_H`)
        for m in re.finditer(rb'(?m)^\s*#\s*define\s+(\w+)', raw):
            own.setdefault(m.group(1).decode(), raw.count(b'\n', 0, m.start()))
        others = set().union(*(DEF[r] for r in visible_files(rel) if r != rel)) | PREDEF
        text = raw.decode('utf-8', 'replace').splitlines()
        out += [dict(e='import', file=rel, line=i + 1, name=l.strip()) for i, l in enumerate(text) if INC.match(l)]
        src = prep(raw)
        st = [p.parse(src).root_node]
        while st:
            n = st.pop()
            if n.type in SKIP: continue
            if n.type in ('preproc_if', 'preproc_elif', 'preproc_ifdef', 'preproc_elifdef'):
                v = _cond(n, others | {k for k, ln in own.items() if ln < n.start_point[0]})
                if v is not None:   # decidable condition: keep only the taken branch, as the preprocessor does
                    alt = n.child_by_field_name('alternative')
                    st.extend([c for c in n.children if c != alt and c.type not in ('preproc_else', 'preproc_elif', 'preproc_elifdef')] if v else ([alt] if alt is not None else []))
                    continue
            if n.type == 'call_expression':
                i = _callee(n.child_by_field_name('function'))
                if i is not None and _macro(i.text.decode(), macros, types):   # a macro / cast: no call row, args still scanned
                    st.extend(c for c in n.children if c.type == 'argument_list'); continue
            st.extend(n.children)
            t = n.type
            if t in CLASS:
                nm, body = n.child_by_field_name('name'), n.child_by_field_name('body')
                if nm is not None and body is not None and nm.type in ('type_identifier', 'identifier'):
                    L = nm.start_point[0] + 1; name = nm.text.decode()
                    out.append(dict(e='class', file=rel, line=L, name=name, kind=t.split('_')[0]))
                    for c in n.children:
                        if c.type == 'base_class_clause':
                            for b in c.named_children:
                                i = _callee(b) if b.type != 'access_specifier' else None
                                if b.type == 'template_type': i = b.child_by_field_name('name')
                                if b.type == 'qualified_identifier':
                                    while b is not None and b.type == 'qualified_identifier': b = b.child_by_field_name('name')
                                    i = b.child_by_field_name('name') if b is not None and b.type == 'template_type' else b
                                if i is not None: out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=i.text.decode()))
            elif t in ('class_interface', 'protocol_declaration'):
                ids = [c for c in n.children if c.type == 'identifier']
                if t == 'class_interface' and any(c.type == '(' for c in n.children): continue  # category
                if not ids: continue
                if t == 'protocol_declaration' and any(c.type == ';' for c in n.children) and not any(c.type == '@end' for c in n.children): continue
                name = ids[0].text.decode(); L = ids[0].start_point[0] + 1
                out.append(dict(e='class', file=rel, line=L, name=name, kind=t))
                sups = [c.text.decode() for c in ids[1:2]] if t == 'class_interface' else []
                for c in n.children:
                    if c.type in ('parameterized_arguments', 'protocol_reference_list'):
                        sups += [x.text.decode() for x in c.named_children if x.type in ('identifier', 'type_identifier')]
                        sups += [y.text.decode() for x in c.named_children if x.type == 'type_name' for y in x.named_children if y.type == 'type_identifier']
                for s in sups: out.append(dict(e='inherits', file=rel, line=L, cls=name, sup=s))
            elif t == 'call_expression':
                i = _callee(n.child_by_field_name('function'))
                if i is not None:
                    out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=n.start_point[0] + 1, name=i.text.decode().split('<')[0]))
            elif t == 'message_expression':
                i = n.child_by_field_name('method') or _first_kw(n)
                if i is not None:
                    out.append(dict(e='call', file=rel, line=n.start_point[0] + 1, start=n.start_point[0] + 1, name=i.text.decode()))
    return out
