"""Edges found by the tree-sitter module (Java first).

module_edges(root, file, lang='java') -> dict of row lists, same shape as java-ref/ListEdges.java:
  contains  file, line, parent, parent_line, child, kind      parent "" = file or anonymous class; kind = class|method
  inherits  file, line, cls, sup, how                         sup = simple name as written, no generics
  import    file, line, name, static, star
  call      file, line, start, name, how                      how = call | new; super(...)/this(...) named super/this
Anonymous classes (new X() {...}, enum constants with a body) are listed with child "" at the line of their "{", as javac.
Enum constants are calls named after the enum (how=new): javac treats each one as new Enum(...).
"""
from pathlib import Path

# Rules, each switchable so the notebooks can prove what it fixes (default: all on).
RULES = {
    1: 'annotation types (@interface) are classes',
    2: 'anonymous classes (new X() {...}, enum constant bodies) are classes, at the line of their "{"',
    3: 'annotation members (String value();) are methods',
    4: 'enum constants are calls new Enum(...), at the line of their name',
    5: 'super(...) / this(...) are calls named super / this',
}
ALL_RULES = frozenset(RULES)
from tree_sitter_language_pack import get_parser

CLASS = {'class_declaration': 'class', 'interface_declaration': 'interface', 'enum_declaration': 'enum',
         'record_declaration': 'record', 'annotation_type_declaration': 'annotation'}
METHOD = {'method_declaration', 'constructor_declaration', 'compact_constructor_declaration',
          'annotation_type_element_declaration'}


def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _simple(type_node):
    """Simple name of a type as written: Map.Entry<K, V> -> Entry, @Ann Foo -> Foo."""
    n = type_node
    while n is not None and n.type in ('generic_type', 'annotated_type'):
        n = next((c for c in n.named_children if c.type not in ('type_arguments', 'annotation', 'marker_annotation')), None)
    return _t(n).split('.')[-1].strip() if n is not None else ''


def _types_in(n):
    """Types listed in super_interfaces / extends_interfaces / superclass."""
    out = []
    for c in n.named_children:
        if c.type == 'type_list':
            out += c.named_children
        elif c.type not in ('annotation', 'marker_annotation'):
            out.append(c)
    return out


def module_edges(root, file, lang='java', rules=ALL_RULES):
    if lang == 'javascript':          # rules 1-5 are Java rules; JS / Python edges below
        return _js_edges(root, file)
    if lang == 'python':
        return _py_edges(root, file)
    if lang != 'java':
        raise ValueError(f'edges: only java so far, got {lang}')
    src = (Path(root) / file).read_bytes()
    tree = get_parser('java').parse(src)
    E = {'contains': [], 'inherits': [], 'import': [], 'call': []}
    line = lambda n: n.start_point[0] + 1
    cls_kinds = CLASS if 1 in rules else {k: v for k, v in CLASS.items() if k != 'annotation_type_declaration'}
    meth_kinds = METHOD if 3 in rules else METHOD - {'annotation_type_element_declaration'}

    def walk(n, parent):
        t = n.type
        if t in cls_kinds or (2 in rules and t == 'class_body' and n.parent is not None and n.parent.type in ('object_creation_expression', 'enum_constant')):
            anon = t == 'class_body'                  # new X() {...}  or  enum constant with a body
            name = '' if anon else _t(n.child_by_field_name('name'))
            ln = line(n)                              # anonymous: line of '{', as javac
            E['contains'].append(dict(file=file, line=ln, parent=parent[0], parent_line=parent[1], child=name, kind='class'))
            if not anon:
                for field, how in (('superclass', 'extends'), ('interfaces', 'implements')):
                    s = n.child_by_field_name(field)
                    if s is not None:
                        h = 'extends' if t == 'interface_declaration' else how
                        for ty in _types_in(s):
                            E['inherits'].append(dict(file=file, line=ln, cls=name, sup=_simple(ty), how=h))
                for c in n.named_children:
                    if c.type == 'extends_interfaces':
                        for ty in _types_in(c):
                            E['inherits'].append(dict(file=file, line=ln, cls=name, sup=_simple(ty), how='extends'))
            parent = (name, ln)
        elif t in meth_kinds:
            name = parent[0] if t in ('constructor_declaration', 'compact_constructor_declaration') else _t(n.child_by_field_name('name'))
            E['contains'].append(dict(file=file, line=line(n), parent=parent[0], parent_line=parent[1], child=name, kind='method'))
        elif t == 'import_declaration':
            kids = [c for c in n.named_children]
            name = ''.join(_t(c) for c in kids if c.type in ('identifier', 'scoped_identifier'))
            star = any(c.type == 'asterisk' for c in n.children)
            E['import'].append(dict(file=file, line=line(n), name=name + ('.*' if star else ''),
                                    static=any(c.type == 'static' for c in n.children), star=star))
            return
        elif t == 'method_invocation':
            nm = n.child_by_field_name('name')
            E['call'].append(dict(file=file, line=line(nm), start=line(n), name=_t(nm), how='call'))
        elif 5 in rules and t == 'explicit_constructor_invocation':
            c = n.child_by_field_name('constructor')
            E['call'].append(dict(file=file, line=line(n), start=line(n), name=_t(c), how='call'))
        elif 4 in rules and t == 'enum_constant':                    # javac: every enum constant is new Enum(...)
            nl = line(n.child_by_field_name('name'))   # after any annotation, as javac
            E['call'].append(dict(file=file, line=nl, start=nl, name=parent[0], how='new'))
        elif t == 'object_creation_expression':
            E['call'].append(dict(file=file, line=line(n), start=line(n), name=_simple(n.child_by_field_name('type')), how='new'))
        for c in n.children:
            walk(c, parent)

    import sys
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    walk(tree.root_node, ('', 0))
    return E


def _contains_from_module(root, file, lang):
    """CONTAINS children = the module's own named functions (extract.py) and classes (classes.py)."""
    from extract import module_functions
    from classes import module_classes
    rows = [dict(file=file, line=f['line_start'], child=f['name'], kind='function')
            for f in module_functions(Path(root), file, lang) if f['named']]
    rows += [dict(file=file, line=c['line_start'], child=c['name'], kind=c['kind'])
             for c in module_classes(root, file, lang) if c['name']]
    return rows


def _walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


def _js_edges(root, file):
    """Same shape as js-ref/edges.mjs (TypeScript compiler)."""
    src = (Path(root) / file).read_bytes()
    tree = get_parser('javascript').parse(src)
    E = {'contains': _contains_from_module(root, file, 'javascript'), 'inherits': [], 'import': [], 'call': []}
    line = lambda n: n.start_point[0] + 1
    def last(e):
        if e is None: return None
        if e.type == 'identifier': return e
        if e.type == 'member_expression': return e.child_by_field_name('property')
        return None
    def string_arg(args):
        a = args.named_children[0] if args is not None and args.named_children else None
        return a if a is not None and a.type in ('string', 'template_string') and not any(c.type == 'template_substitution' for c in a.children) else None
    unq = lambda s: _t(s)[1:-1]
    for n in _walk(tree.root_node):
        t = n.type
        if t in ('class_declaration', 'class'):
            for h in n.named_children:
                if h.type == 'class_heritage':
                    e = h.named_children[0] if h.named_children else None
                    i = last(e)
                    E['inherits'].append(dict(file=file, line=line(n), cls=_t(n.child_by_field_name('name')), sup=_t(i) if i else _t(e)))
        elif t in ('import_statement', 'export_statement') and n.child_by_field_name('source') is not None:
            E['import'].append(dict(file=file, line=line(n), name=unq(n.child_by_field_name('source')), how='import'))
        elif t == 'call_expression':
            f, args = n.child_by_field_name('function'), n.child_by_field_name('arguments')
            if args is not None and args.type == 'template_string':
                continue                                  # tag`...` is a tagged template, not a call (as TS)
            a = string_arg(args)
            if f.type == 'identifier' and _t(f) == 'require' and a is not None:
                E['import'].append(dict(file=file, line=line(n), name=unq(a), how='require'))
            elif f.type == 'import' and a is not None:
                E['import'].append(dict(file=file, line=line(n), name=unq(a), how='dynamic'))
            i = last(f)
            if i is not None:
                E['call'].append(dict(file=file, line=line(i), start=line(n), name=_t(i), how='call'))
        elif t == 'new_expression':
            i = last(n.child_by_field_name('constructor'))
            if i is not None:
                E['call'].append(dict(file=file, line=line(i), start=line(n), name=_t(i), how='new'))
        elif t in ('jsx_self_closing_element', 'jsx_opening_element'):
            nm = n.child_by_field_name('name')
            i = nm if nm is not None and nm.type == 'identifier' else last(nm)
            if i is not None:
                html = nm.type == 'identifier' and _t(i)[:1].islower()
                E['call'].append(dict(file=file, line=line(i), start=line(n), name=_t(i), how='jsx_html' if html else 'jsx'))
    return E


def _py_edges(root, file):
    """Same shape as lang-ref/python/edges.py (CPython ast)."""
    src = (Path(root) / file).read_bytes()
    tree = get_parser('python').parse(src)
    E = {'contains': _contains_from_module(root, file, 'python'), 'inherits': [], 'import': [], 'call': []}
    line = lambda n: n.start_point[0] + 1
    def last(e):
        if e.type == 'identifier': return e
        if e.type == 'attribute': return e.child_by_field_name('attribute')
        return None
    for n in _walk(tree.root_node):
        t = n.type
        if t == 'class_definition':
            start = n.parent if n.parent is not None and n.parent.type == 'decorated_definition' else n
            sup = n.child_by_field_name('superclasses')
            for b in (sup.named_children if sup is not None else []):
                if b.type in ('keyword_argument', 'comment', 'list_splat', 'dictionary_splat'):
                    continue
                i = last(b)
                E['inherits'].append(dict(file=file, line=line(start), def_line=line(n), cls=_t(n.child_by_field_name('name')),
                                          sup=_t(i) if i is not None else _t(b)))
        elif t == 'import_statement':
            for c in n.named_children:
                nm = c.child_by_field_name('name') if c.type == 'aliased_import' else c
                E['import'].append(dict(file=file, line=line(n), name=_t(nm), how='import'))
        elif t == 'import_from_statement':
            m = n.child_by_field_name('module_name')
            E['import'].append(dict(file=file, line=line(n), name=_t(m), how='from'))
        elif t == 'future_import_statement':
            E['import'].append(dict(file=file, line=line(n), name='__future__', how='from'))
        elif t == 'call':
            f = n.child_by_field_name('function')
            i = last(f)
            if i is not None:
                E['call'].append(dict(file=file, line=line(i), start=line(n), name=_t(i)))
    return E
