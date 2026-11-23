"""Module (tree-sitter) classes and edges for TypeScript / TSX / Vue / Svelte. Same row contract and conventions as
edgeref_typescript.py (lang-ref/sfc/edges.mjs): classes + interfaces + enums, extends/implements, one import row per
import/export-from statement / require('m') / import('m') / import x = require('m'), calls + new + JSX elements.
.vue/.svelte: each <script> block parsed alone (lang="ts" -> typescript grammar), lines shifted to the file."""
import re
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = "tree-sitter typescript/tsx/javascript (+ vue/svelte <script> blocks): class/interface/enum, heritage, imports, call/new/jsx"

def _t(n): return n.text.decode('utf-8', 'replace') if n is not None else ''

def _last(e):
    if e is None: return None
    if e.type in ('identifier', 'type_identifier', 'property_identifier'): return e
    if e.type == 'member_expression': return e.child_by_field_name('property')
    if e.type == 'nested_type_identifier': return e.child_by_field_name('name')
    return None

def _strip(e):   # expression with type args (Foo<T>) -> Foo
    while e is not None and e.type in ('generic_type', 'instantiation_expression', 'expression_with_type_arguments'):
        e = e.named_children[0] if e.named_children else None
    return e

def _blocks(rel, src):
    if rel.endswith(('.vue', '.svelte')):
        doc = get_parser('vue' if rel.endswith('.vue') else 'svelte').parse(src).root_node
        out = []
        for el in doc.named_children:
            if el.type != 'script_element': continue
            tag = next((c for c in el.children if c.type == 'start_tag'), None)
            raw = next((c for c in el.children if c.type == 'raw_text'), None)
            if raw is None: continue
            m = re.search(r'lang\s*=\s*["\']?(tsx|ts|typescript|jsx)', _t(tag))
            g = {'tsx': 'tsx', 'jsx': 'javascript'}.get(m.group(1), 'typescript') if m else 'javascript'
            out.append((raw.text, raw.start_point[0], g))
        return out
    g = 'tsx' if rel.endswith('.tsx') else 'typescript' if re.search(r'\.[mc]?ts$', rel) else 'javascript'
    return [(src, 0, g)]

def _strarg(args):
    a = args.named_children[0] if args is not None and args.named_children else None
    if a is not None and a.type == 'string': return a
    if a is not None and a.type == 'template_string' and not any(c.type == 'template_substitution' for c in a.children): return a
    return None

def _cname(n):
    nm = n.child_by_field_name('name')
    if nm is not None: return _t(nm)
    p = n.parent
    if p is None: return ''
    if p.type in ('variable_declarator', 'pair'):
        k = p.child_by_field_name('name') or p.child_by_field_name('key')
        return _t(k) if k is not None and k.type in ('identifier', 'property_identifier') else ''
    if p.type == 'assignment_expression':
        l = p.child_by_field_name('left'); i = _last(l)
        return _t(i) if i is not None else ''
    return ''

CLS = {'class_declaration': 'class', 'abstract_class_declaration': 'class', 'class': 'class_expression',
       'interface_declaration': 'interface', 'enum_declaration': 'enum'}

def _start(n):   # TS getStart: decorators belong to the class node in TS, export keyword does not
    return n.start_point[0]

def edges(root, files):
    out = []
    for rel in files:
        try: src = (Path(root) / rel).read_bytes()
        except Exception: continue
        for text, off, g in _blocks(rel, src):
            tree = get_parser(g).parse(text)
            L = lambda n: n.start_point[0] + 1 + off
            st = [tree.root_node]
            while st:
                n = st.pop(); st.extend(reversed(n.children)); t = n.type
                if t in CLS:
                    name = _cname(n)
                    if name: out.append(dict(e='class', file=rel, line=L(n), name=name, kind=CLS[t]))
                    sups = []
                    for h in n.named_children:
                        if h.type == 'class_heritage':
                            for c in h.named_children:
                                if c.type in ('extends_clause', 'implements_clause'):
                                    sups += [x for x in c.named_children if x.type != 'type_arguments']
                                else: sups.append(c)   # javascript grammar: class_heritage -> expression
                        elif h.type == 'extends_type_clause':
                            sups += [x for x in h.named_children if x.type != 'type_arguments']
                    for s in sups:
                        s2 = _strip(s); i = _last(s2)
                        out.append(dict(e='inherits', file=rel, line=L(n), cls=name, sup=_t(i) if i is not None else _t(s2)))
                elif t in ('import_statement', 'export_statement'):
                    src_ = n.child_by_field_name('source')
                    if src_ is not None: out.append(dict(e='import', file=rel, line=L(n), name=_t(src_)[1:-1]))
                    else:
                        r = next((c for c in n.named_children if c.type == 'import_require_clause'), None)
                        if r is not None:
                            s = r.child_by_field_name('source') or next((c for c in r.named_children if c.type == 'string'), None)
                            if s is not None: out.append(dict(e='import', file=rel, line=L(n), name=_t(s)[1:-1]))
                elif t == 'call_expression':
                    f, args = n.child_by_field_name('function'), n.child_by_field_name('arguments')
                    if args is not None and args.type == 'template_string': continue
                    a = _strarg(args)
                    if a is not None and ((f.type == 'identifier' and _t(f) == 'require') or f.type == 'import'):
                        out.append(dict(e='import', file=rel, line=L(n), name=_t(a)[1:-1]))
                    i = _last(_strip(f))
                    if i is not None and i.type != 'type_identifier':
                        out.append(dict(e='call', file=rel, line=L(i), start=L(n), name=_t(i)))
                elif t == 'new_expression':
                    i = _last(_strip(n.child_by_field_name('constructor')))
                    if i is not None: out.append(dict(e='call', file=rel, line=L(i), start=L(n), name=_t(i)))
                elif t in ('jsx_self_closing_element', 'jsx_opening_element'):
                    i = _last(n.child_by_field_name('name'))
                    if i is not None: out.append(dict(e='call', file=rel, line=L(i), start=L(n), name=_t(i)))
    return out
