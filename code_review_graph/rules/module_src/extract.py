"""Step 1 of the core module: find every function in one file.
Entry point: module_functions(root: Path, rel: str, lang: str) -> list[dict]
Each row: file, name, line_start, line_end, named, scope, label, arity, byte_start, byte_end.
Copied unchanged from thirdset-lab/module/t2 (c4_module_core, c5_legacy, c6_generic, c7_rules, c8_table)."""

# ======== c4_module_core.py ========
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from tree_sitter_language_pack import get_parser

sys.setrecursionlimit(20000)   # deep trees walk recursively


# ---------- small tree helpers (all languages) ----------

def _t(n):
    return n.text.decode('utf-8', 'replace') if n is not None else ''


def _f(n, *fields):
    """First child found under any of these field names."""
    if n is None:
        return None
    for name in fields:
        c = n.child_by_field_name(name)
        if c is not None:
            return c
    return None


def _kid(n, *types):
    """First named child of one of these types."""
    if n is None:
        return None
    for c in n.named_children:
        if c.type in types:
            return c
    return None


def _desc(n, types, skip=()):
    """First descendant (breadth first) of one of these types, not entering `skip` types."""
    todo = list(n.named_children) if n is not None else []
    while todo:
        c = todo.pop(0)
        if c.type in types:
            return c
        if c.type not in skip:
            todo.extend(c.named_children)
    return None


IDENT = re.compile(r'[A-Za-z_][\w!?$]*')


def _last_ident(text):
    """`router.post` -> post, `std::sort` -> sort, `M:b` -> b, `List<int>.of` -> of."""
    found = IDENT.findall(re.sub(r'<[^<>]*>', '', text[:200]))
    return found[-1] if found else ''


def _unq(s):
    return s.strip().strip('\'"`')


def _up(n, wrappers=('parenthesized_expression', 'as_expression', 'satisfies_expression',
                     'non_null_expression', 'type_assertion')):
    p = n.parent
    while p is not None and p.type in wrappers:
        p = p.parent
    return p

# ======== c5_legacy.py ========
FUNC = {
    'javascript': {'function_declaration', 'generator_function_declaration', 'function_expression', 'function',
                   'generator_function', 'arrow_function', 'method_definition'},
    'python': {'function_definition', 'lambda'},
    'java': {'method_declaration', 'constructor_declaration', 'compact_constructor_declaration',
             'annotation_type_element_declaration', 'lambda_expression'},
}
FUNC['typescript'] = FUNC['tsx'] = FUNC['javascript'] | {'method_signature', 'function_signature', 'abstract_method_signature'}
ALWAYS_NAMED = {'function_declaration', 'generator_function_declaration', 'method_definition', 'method_signature',
                'function_signature', 'abstract_method_signature', 'function_definition', 'method_declaration',
                'constructor_declaration', 'compact_constructor_declaration', 'annotation_type_element_declaration'}
SCOPE = {'class_declaration', 'class', 'abstract_class_declaration', 'interface_declaration', 'enum_declaration',
         'record_declaration', 'annotation_type_declaration', 'class_definition'}
CALLS = {'call_expression', 'method_invocation', 'call', 'new_expression'}
ARGS = {'arguments', 'argument_list'}
WRAPPERS = {'useCallback', 'memo', 'forwardRef'}
ROUTES = {'get', 'post', 'put', 'patch', 'delete', 'all', 'use', 'head', 'options'}
TESTS = {'describe', 'it', 'test', 'context', 'suite', 'specify'}
STRINGS = {'string', 'string_literal', 'template_string'}


def _up_js(n):  # the original JS walk: kept exactly as in treesitter-module.ipynb
    """The parent, skipping parentheses and TS `as` / `satisfies` wrappers."""
    p = n.parent
    while p is not None and p.type in ('parenthesized_expression', 'as_expression', 'satisfies_expression'):
        p = p.parent
    return p


def _callee(call):
    """(name, object text) of a call: `router.post` -> ('post', 'router'), `useEffect` -> ('useEffect', '')."""
    f = call.child_by_field_name('function') or call.child_by_field_name('constructor')
    if call.type == 'method_invocation':
        return _t(call.child_by_field_name('name')), _t(call.child_by_field_name('object'))
    if f is None:
        return '', ''
    if f.type in ('member_expression', 'attribute'):
        prop = f.child_by_field_name('property') or f.child_by_field_name('attribute')
        return _t(prop), _t(f.child_by_field_name('object'))
    return _t(f), ''


def _first_string(args):
    for a in args.named_children:
        if a.type in STRINGS:
            return _t(a).strip('\'"`')
    return None


def _key_text(k):
    return _t(k).strip('\'"`') if k is not None and k.type in ('property_identifier', 'identifier', 'string',
                                                                 'private_property_identifier', 'number') else None


def name_of(n):
    """The function's name if the code gives it one, else None."""
    if n.type in ALWAYS_NAMED:
        return _t(n.child_by_field_name('name')).strip('\'"`') or None
    if n.type in ('lambda', 'lambda_expression'):   # Python / Java lambdas: the references count them as unnamed
        return None
    p = _up_js(n)
    if p is None:
        return None
    if p.type == 'variable_declarator':
        return _t(p.child_by_field_name('name')) or None
    if p.type in ('assignment_expression', 'augmented_assignment_expression'):
        left = p.child_by_field_name('left')
        if left is not None and left.type == 'member_expression':
            return _key_text(left.child_by_field_name('property'))
        return _t(left) if left is not None and left.type == 'identifier' else None
    if p.type == 'pair':
        return _key_text(p.child_by_field_name('key'))
    if p.type in ('field_definition', 'public_field_definition'):
        return _key_text(p.child_by_field_name('property') or p.child_by_field_name('name'))
    if p.type in ARGS:                                # f = useCallback(() => …), memo(…), forwardRef(…)
        call = _up_js(p)
        if call is not None and call.type in CALLS and _callee(call)[0] in WRAPPERS:
            q = _up_js(call)
            if q is not None and q.type == 'variable_declarator':
                return _t(q.child_by_field_name('name')) or None
    return None


JSX_HINTS = ('id', 'aria-label', 'data-testid', 'name', 'title')
EFFECT_HOOKS = ('useEffect', 'useLayoutEffect', 'useInsertionEffect')


def _jsx_label(attr):
    """<Button "Previous">.onClick — element tag, a human hint (id / aria-label / data-testid / name /
    title, else the element's own text), then the attribute."""
    opening = attr.parent                                   # jsx_opening_element / jsx_self_closing_element
    tag = _t(opening.child_by_field_name('name')) or '?'
    hint = None
    for a in opening.named_children:
        if a.type == 'jsx_attribute' and a.named_children and _t(a.named_children[0]) in JSX_HINTS:
            v = a.named_children[-1]
            if v.type == 'string':
                hint = _t(v).strip('\'"'); break
    if hint is None and opening.type == 'jsx_opening_element' and opening.parent is not None:
        texts = [_t(c).strip() for c in opening.parent.named_children if c.type == 'jsx_text' and _t(c).strip()]
        hint = texts[0] if texts else None
    attr_name = _t(attr.named_children[0]) if attr.named_children else 'attr'
    return f'<{tag} "{hint[:40]}">.{attr_name}' if hint else f'<{tag}>.{attr_name}'


def label_of(n, scope=()):
    """A context label for a function with no name."""
    p = _up_js(n)
    if p is None:
        return n.type
    if p.type == 'jsx_expression' and p.parent is not None and p.parent.type == 'jsx_attribute':
        return _jsx_label(p.parent)
    if p.type in ('return_statement', 'arrow_function') and n.parent is not None:
        # return () => … / useEffect(() => () => …): the cleanup when the enclosing function is an effect
        return 'cleanup' if scope and scope[-1].split('#')[0] in EFFECT_HOOKS else 'returned function'
    if p.type in ARGS:
        call = _up_js(p)
        if call is not None and call.type in CALLS:
            name, _obj = _callee(call)
            s = _first_string(p)
            if name.lower() in ROUTES and s and s.startswith('/'):
                return f'{name.upper()} {s} handler'
            if s is not None:                         # describe('save'), it('fails'), setattr('_grade'), on('data')
                return f"{name or 'callback'}('{s[:60]}')"
            return name or 'callback'
    if p.type in CALLS and p.child_by_field_name('function') is not None and n.start_byte <= p.child_by_field_name('function').end_byte:
        return 'iife'
    if p.type == 'assignment' and n.type == 'lambda':          # Python: h = lambda x: …
        return f"lambda {_t(p.child_by_field_name('left'))}"
    if p.type == 'export_statement':
        return 'default export'
    if n.type == 'function_expression' and n.child_by_field_name('name') is not None:
        return f"function {_t(n.child_by_field_name('name'))}"
    return {'arrow_function': 'arrow', 'function_expression': 'function', 'lambda': 'lambda',
            'lambda_expression': 'lambda'}.get(n.type, n.type)


def arity(n):
    """The signature part of the identity: Java = parameter types (overloads differ by type), else the count."""
    if n.type in ('method_declaration', 'constructor_declaration'):
        ps = n.child_by_field_name('parameters')
        return ','.join(_t(c.child_by_field_name('type')) or c.type for c in (ps.named_children if ps else [])
                        if c.type in ('formal_parameter', 'spread_parameter'))
    ps = n.child_by_field_name('parameters')
    if ps is None:
        ps = n.child_by_field_name('parameter')   # x => …
        return 1 if ps is not None else 0
    return sum(1 for c in ps.named_children if c.type not in ('comment',))


def legacy_functions(src, grammar, rel, line_offset=0):
    """JS / TS / TSX / Python / Java: the walk from treesitter-module.ipynb, unchanged except that it takes the
    source bytes (so a Svelte/Vue <script> or a notebook's joined cells can be passed) and a line offset."""
    # tree-sitter's JS scanner reads `<!--` as an HTML-like comment even inside template strings / JSX text,
    # which turns whole files into ERROR. Blank it (same length, lines kept) before parsing.
    src = src.replace(b'<!--', b'    ') if isinstance(src, bytes) else src
    tree = get_parser(grammar).parse(src)
    funcs, kinds = [], FUNC[grammar]
    counters = defaultdict(Counter)            # scope tuple -> label -> count

    def walk(node, scope):
        new_scope = scope
        if node.is_named and node.type in kinds:
            start, end = node.start_point[0] + 1 + line_offset, node.end_point[0] + 1 + line_offset
            name = name_of(node)
            if name:
                funcs.append({'file': rel, 'name': name, 'line_start': start, 'line_end': end, 'byte_start': node.start_byte, 'byte_end': node.end_byte, 'named': True,
                              'scope': '.'.join(scope), 'label': name, 'arity': arity(node)})
                new_scope = scope + (name,)
            else:
                label = label_of(node, scope)
                counters[scope][label] += 1
                label = f'{label}#{counters[scope][label]}'
                funcs.append({'file': rel, 'name': label, 'line_start': start, 'line_end': end, 'byte_start': node.start_byte, 'byte_end': node.end_byte, 'named': False,
                              'scope': '.'.join(scope), 'label': label, 'arity': arity(node)})
                new_scope = scope + (label,)
        elif node.type in SCOPE:
            cname = _t(node.child_by_field_name('name'))
            if cname:
                new_scope = scope + (cname,)
        elif node.type == 'pair' and getattr(node.child_by_field_name('value'), 'type', '') == 'object':
            key = _key_text(node.child_by_field_name('key'))
            if key:
                new_scope = scope + (key,)            # phone: { countryCode: { validate: { validator() {} } } }
        elif node.type == 'variable_declarator' and getattr(node.child_by_field_name('value'), 'type', '') == 'object':
            new_scope = scope + (_t(node.child_by_field_name('name')),)   # const api = { save() {} } -> api.save
        for c in node.children:
            walk(c, new_scope)

    walk(tree.root_node, ())
    return funcs


def with_identity(df):
    """identity = file::scope.name(arity), plus @line only where two functions still share it."""
    if df.empty:
        return df.assign(graph_key='', identity='')
    q = df.scope.where(df.scope == '', df.scope + '.') + df.name
    df = df.assign(graph_key=df.file + '::' + q)
    key = df.graph_key + '(' + df.arity.astype(str) + ')'
    clash = key.duplicated(keep=False)
    ident = key.where(~clash, key + '@' + df.line_start.astype(str))
    # several on one line (minified code, one-line classes): add an ordinal so every identity is unique
    n = ident.groupby(ident).cumcount()
    still = ident.duplicated(keep=False)
    return df.assign(identity=ident.where(~still, ident + '.' + (n + 1).astype(str)))

# ======== c6_generic.py ========
import re
# ---------- the generic walk (every tree-sitter language except the JS family / Python / Java) ----------
#
# A language rule (RULES[lang], next cell) says, for one grammar:
#   classify(n) -> None                   not a function, not a scope
#                  ('fn', name, extra)    a function; name None = unnamed (it may still get a name from context);
#                                         extra = scope parts the code itself gives (Foo::bar -> ('Foo',))
#                  ('scope', name)        a class / module / namespace / test block: its name joins the scope path
#   calls      node types that are calls (for the label of a callback)
#   sig(n)     the signature part of the identity: parameter types where the language overloads by type, else the count
#   body_sibling  node types that hold a function's body as the NEXT sibling of its signature (Dart)
#   label(n)   optional: a language-specific context label, tried before the generic one

ROUTE_VERBS = {'get', 'post', 'put', 'patch', 'delete', 'all', 'use', 'head', 'options'}
CALLEE_FIELDS = ('function', 'method', 'name', 'target', 'callee', 'constructor', 'macro')
STRING_TYPES = ('string', 'string_literal', 'interpreted_string_literal', 'raw_string_literal', 'simple_symbol',
                'symbol', 'quoted_string', 'string_expression', 'template_string', 'str', 'raw_string')
# nodes between a callback and the call it is passed to
ARG_WRAPPERS = {'arguments', 'argument_list', 'argument', 'value_argument', 'value_arguments', 'call_suffix',
                'annotated_lambda', 'lambda_argument', 'block_argument', 'expression_list', 'args', 'call_arguments',
                'FnCallArguments', 'actual_arguments', 'arguments_list'}
VALUE_WRAPPERS = ('parenthesized_expression', 'as_expression', 'satisfies_expression', 'non_null_expression',
                  'expression_list', 'expression', 'value', 'primary_expression', 'unary_expression')
DEFAULT_LABEL = {'lambda_expression': 'lambda', 'lambda': 'lambda', 'closure_expression': 'closure',
                 'func_literal': 'func', 'anonymous_function': 'fn', 'block_literal': 'block', 'lambda_literal': 'lambda',
                 'function_definition': 'function', 'function_expression': 'function', 'block': 'block',
                 'do_block': 'block', 'script_block_expression': 'scriptblock', 'anonymous_subroutine_expression': 'sub',
                 'arrow_function': 'arrow', 'arrow_function_expression': 'arrow', 'anonymous_method_expression': 'delegate',
                 'function_literal': 'function'}


def value_name(n):
    """The name a function VALUE gets from where it is stored, else None.
    `f = …` / `local f = …` / `M.f = …` / `{ f = … }` / `f <- function` / `f = x: …` (Nix) / `let f = |x|` …"""
    child, p = n, n.parent
    while p is not None and p.type in VALUE_WRAPPERS:
        child, p = p, p.parent
    if p is None:
        return None
    t = p.type
    if t in ('variable_declarator', 'init_declarator', 'initialized_variable_definition', 'variable_statement',
             'const_declaration', 'let_declaration', 'val_definition', 'var_definition', 'var_spec', 'const_spec'):
        target = _f(p, 'name', 'declarator', 'pattern') or (p.named_children[0] if p.named_children else None)
        if target is None or target == child:
            return None
        return _last_ident(_t(target)) or None
    if t in ('assignment', 'assignment_expression', 'augmented_assignment_expression', 'assignment_statement',
             'short_var_declaration'):
        left = _f(p, 'left', 'lhs')
        if left is None and p.named_children:
            left = p.named_children[0]
        if left is None or left == child:
            return None
        if left.type in ('variable_list', 'expression_list'):          # Lua `a, b = f1, f2` / Go `a, b := …`
            vals = child.parent.named_children if child.parent is not None and child.parent.type in (
                'expression_list',) else [child]
            idx = next((i for i, v in enumerate(vals) if v == child or v == n), 0)
            targets = [c for c in left.named_children if c.type != 'comment']
            left = targets[idx] if idx < len(targets) else None
            if left is None:
                return None
        if left.type in ('bracket_index_expression', 'subscript_expression', 'subscript', 'element_reference'):
            return None
        return _last_ident(_t(left)) or None
    if t == 'binary_operator':                                         # R: f <- function, function -> f
        op = _t(_f(p, 'operator'))
        lhs, rhs = _f(p, 'lhs'), _f(p, 'rhs')
        if op in ('<-', '<<-', '=', ':=') and rhs is not None and rhs == child and lhs is not None:
            return _unq(_t(lhs)) or None
        if op in ('->', '->>') and lhs is not None and lhs == child and rhs is not None:
            return _unq(_t(rhs)) or None
        return None
    if t == 'pair':
        k = _f(p, 'key')
        return _unq(_t(k)) if k is not None and k.type in ('property_identifier', 'identifier', 'string') else None
    if t == 'field':                                                   # Lua table constructor { f = function … }
        k = _f(p, 'name')
        return _t(k) if k is not None and k.type == 'identifier' else None
    if t == 'binding':                                                 # Nix  f = x: …
        ap = _f(p, 'attrpath')
        if ap is None:
            return None
        last = _t(ap).split('.')[-1].strip().strip('"')               # Nix idents allow - and '
        return last if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'-]*", last) else (_last_ident(_t(ap)) or None)
    if t in ('property_declaration', 'field_declaration', 'public_field_definition', 'field_definition'):
        tgt = _desc(p, ('variable_declaration', 'pattern', 'simple_identifier', 'property_identifier', 'identifier'))
        return _last_ident(_t(tgt)) if tgt is not None and tgt != child else None
    return None


def _call_of(n, R):
    """The call a callback is passed to (climbing through argument wrappers), else None."""
    c, p = n, n.parent
    for _ in range(5):
        if p is None:
            return None
        if p.type in R['calls']:
            # a trailing block / lambda is part of the call; the callee itself is not a callback
            callee = _f(p, *CALLEE_FIELDS)
            if callee is not None and callee == c:
                return None
            return p
        if p.type not in ARG_WRAPPERS:
            return None
        c, p = p, p.parent
    return None


def _first_string(node, limit=60):
    if node is None:
        return None
    for c in node.named_children:
        x = c
        while x is not None and x.type in ('argument', 'value_argument', 'expression') and x.named_children:
            x = x.named_children[0]
        if x is not None and x.type in STRING_TYPES:
            s = _unq(_t(x)).lstrip(':')
            return s[:limit]
    return None


def callee_name(call):
    f = _f(call, *CALLEE_FIELDS)
    if f is None and call.named_children:
        f = call.named_children[0]
    if f is None:
        return ''
    for fld in ('property', 'field', 'name', 'right', 'attribute', 'method'):
        g = _f(f, fld)
        if g is not None:
            return _last_ident(_t(g))
    return _last_ident(_t(f))


def generic_label(n, scope, R):
    """A context label for a function with no name."""
    custom = R.get('label')
    if custom is not None:
        lab = custom(n, scope)
        if lab:
            return lab
    call = _call_of(n, R)
    if call is not None:
        name = callee_name(call)
        s = _first_string(_f(call, 'arguments', 'args') or call)
        if name.lower() in ROUTE_VERBS and s and s.startswith('/'):
            return f'{name.upper()} {s} handler'
        if s is not None:
            return f"{name or 'callback'}('{s}')"
        return name or 'callback'
    p = _up(n)
    if p is not None and p.type in ('return_statement', 'return', 'return_expression'):
        return 'returned function'
    return DEFAULT_LABEL.get(n.type, n.type)


def generic_sig(n, typed=False):
    """Parameter count, or (typed=True) the parameter types joined: overloads differ by type."""
    ps = _f(n, 'parameters', 'parameter_list', 'params')
    if ps is None:
        ps = next((c for c in n.named_children if 'param' in c.type.lower() and c.type not in ('parameter',)), None)
    if ps is None:
        return 0
    items = [c for c in ps.named_children if c.type not in ('comment', '(', ')', ',')]
    if not typed:
        return len(items)
    out = []
    for c in items:
        ty = _f(c, 'type')
        out.append(re.sub(r'\s+', '', _t(ty) if ty is not None else _t(c)))
    return ','.join(out)


def generic_functions(src, lang, rel, line_offset=0):
    """Every function in one file for a RULES language: named and unnamed, each with a scope path."""
    R = RULES[lang]
    if R.get('prep'):
        src = R['prep'](src)
    tree = get_parser(R['grammar']).parse(src)
    funcs = []
    counters = defaultdict(Counter)            # scope tuple -> label -> count
    body_sibling = R.get('body_sibling', ())
    sig = R.get('sig') or (lambda n: generic_sig(n))

    def visit(node, scope):
        """Walk node; return the scope its function opened (for a Dart body sibling), else None."""
        kind = R['classify'](node) if node.is_named else None
        new_scope, opened = scope, None
        if kind and kind[0] == 'fn':
            _, name, extra = kind
            start = (R['start'](node) if R.get('start') else node.start_point[0]) + 1 + line_offset
            end = node.end_point[0] + 1 + line_offset
            fscope = scope + tuple(extra or ())
            if name is None:
                name = value_name(node)
            if name:
                rec = {'file': rel, 'name': name, 'line_start': start, 'line_end': end, 'byte_start': node.start_byte, 'byte_end': node.end_byte, 'named': True,
                       'scope': '.'.join(fscope), 'label': name, 'arity': sig(node)}
                new_scope = fscope + (name,)
            else:
                label = generic_label(node, scope, R)
                counters[fscope][label] += 1
                label = f'{label}#{counters[fscope][label]}'
                rec = {'file': rel, 'name': label, 'line_start': start, 'line_end': end, 'byte_start': node.start_byte, 'byte_end': node.end_byte, 'named': False,
                       'scope': '.'.join(fscope), 'label': label, 'arity': sig(node)}
                new_scope = fscope + (label,)
            funcs.append(rec)
            opened = (new_scope, rec)
        elif kind and kind[0] == 'scope' and kind[1]:
            new_scope = scope + tuple(p for p in kind[1].split('.') if p) if isinstance(kind[1], str) else scope + tuple(kind[1])
        kids = node.children
        i = 0
        while i < len(kids):
            got = visit(kids[i], new_scope)
            if got and i + 1 < len(kids) and kids[i + 1].type in body_sibling:   # Dart: signature, then its body
                body = kids[i + 1]
                got[1]['line_end'] = body.end_point[0] + 1 + line_offset
                got[1]['byte_end'] = body.end_byte
                visit(body, got[0])
                i += 1
            i += 1
        return opened

    visit(tree.root_node, ())
    if R.get('post'):
        funcs = R['post'](src, rel, funcs, line_offset)
    return funcs

# ======== c7_rules.py ========
# ---------- per-language rules ----------
# Each classify() follows that language's reference: a function the reference names must come out named, with the
# same name, starting on a line inside the reference's range. Everything else that is a function comes out unnamed.

def _scope_name(n, *fields):
    x = _f(n, *(fields or ('name',)))
    if x is None:
        x = _kid(n, 'identifier', 'type_identifier', 'simple_identifier', 'constant', 'name', 'alias',
                 'namespace_identifier', 'class_identifier', 'simple_name', 'scope_resolution')
    return _unq(_t(x)) if x is not None else None


def _scoped(scopes, fields=('name',)):
    def classify(n):
        if n.type in scopes:
            return ('scope', _scope_name(n, *fields))
        return None
    return classify


# --- C / C++ / Objective-C (reference: clang AST, definitions with a body; lambdas and blocks unnamed) ---

def _c_declarator_name(d):
    """(name, extra scope) from a C/C++ declarator chain: `Foo::bar`, `~Foo`, `operator==`, `operator bool`."""
    extra = []
    while d is not None:
        t = d.type
        if t in ('function_declarator', 'pointer_declarator', 'reference_declarator', 'attributed_declarator',
                 'parenthesized_declarator', 'array_declarator'):
            nxt = _f(d, 'declarator')
            if nxt is None:
                nxt = next((c for c in d.named_children if c.type not in ('type_qualifier', 'attribute_specifier',
                                                                          'ms_call_modifier')), None)
            d = nxt
            continue
        if t == 'qualified_identifier':
            sc = _f(d, 'scope')
            if sc is not None:
                extra.append(re.sub(r'<.*', '', _t(sc)))
            d = _f(d, 'name')
            continue
        if t == 'template_function':
            d = _f(d, 'name')
            continue
        if t == 'operator_cast':
            return 'operator ' + re.sub(r'\s+', ' ', _t(_f(d, 'type'))).strip(), extra
        if t == 'operator_name':
            op = re.sub(r'\s+', '', _t(d))
            op = re.sub(r'^operator(new|delete)', r'operator \1', op)
            op = re.sub(r'^operator""', 'operator""', op)
            return op, extra
        if t == 'destructor_name':
            return re.sub(r'\s+', '', _t(d)), extra
        if t in ('identifier', 'field_identifier', 'type_identifier'):
            return _t(d), extra
        return None, extra
    return None, extra


C_SCOPES = {'class_specifier', 'struct_specifier', 'union_specifier', 'namespace_definition',
            'class_implementation', 'class_interface', 'category_implementation', 'category_interface',
            'protocol_declaration'}


def classify_c(n):
    t = n.type
    if t == 'function_definition':
        if _f(n, 'body') is None:                 # `= default` / `= delete`: no body, not a definition
            return None
        name, extra = _c_declarator_name(_f(n, 'declarator'))
        return ('fn', name or None, tuple(extra)) if name else ('fn', None, ())
    if t in ('lambda_expression', 'block_literal'):
        return ('fn', None, ())
    if t == 'method_definition':                  # Obj-C: name = first selector keyword (clang's / the graph's rule)
        sel = _kid(n, 'identifier', 'field_identifier')
        return ('fn', _t(sel) if sel is not None else None, ())
    if t in C_SCOPES:
        nm = _f(n, 'name')
        if nm is None:
            nm = _kid(n, 'identifier', 'type_identifier', 'namespace_identifier')
        return ('scope', _t(nm) if nm is not None else None)
    return None


def classify_c_only(n):
    """C: `MACRO(name) { body }` (jemalloc TEST_BEGIN) parses as a definition typed MACRO with a parenthesized
    declarator and no function_declarator; clang reports it under the macro's name, as does the graph."""
    if n.type == 'function_definition' and _f(n, 'body') is not None:
        d, ty = _f(n, 'declarator'), _f(n, 'type')
        if d is not None and d.type == 'parenthesized_declarator' and ty is not None \
                and re.fullmatch(r'[A-Z][A-Z0-9_]+', _t(ty) or ''):
            return ('fn', _t(ty), ())
    return classify_c(n)


def sig_c(n, typed=True):
    if n.type == 'method_definition':             # Obj-C selector: application:didFinish:
        parts = [_t(c) for c in n.named_children if c.type in ('identifier', 'field_identifier')]
        return ':'.join(parts) + (':' if _kid(n, 'method_parameter') is not None else '')
    d = _f(n, 'declarator')
    while d is not None and d.type != 'function_declarator':
        d = _f(d, 'declarator')
    if d is None:
        return generic_sig(n)
    ps = _f(d, 'parameters')
    items = [c for c in (ps.named_children if ps else []) if c.type in (
        'parameter_declaration', 'optional_parameter_declaration', 'variadic_parameter_declaration', 'variadic_parameter')]
    if not typed:
        return len(items)
    return ','.join(re.sub(r'\s+', '', _t(_f(c, 'type')) or c.type) for c in items)


MACRO_SPEC = re.compile(rb'(?m)^(?!\s*#)(.*?)\b([A-Z][A-Z0-9_]{2,})\b(?=\s+[A-Za-z_~]|\s*[{;:=]|\s+[A-Z][A-Z0-9_]{2,}\b)')
NOT_SPEC = {b'struct', b'class', b'enum', b'union', b'namespace', b'return', b'case', b'goto', b'new', b'delete',
            b'sizeof', b'typedef', b'using', b'throw'}


def c_prep(src, keep_ret_type=False):
    """Blank ALL-CAPS macro specifiers (`FMT_CONSTEXPR`, `SPDLOG_INLINE`, `SPDLOG_NOEXCEPT {`) with spaces, keeping every
    line and column: tree-sitter cannot expand macros and misreads the declaration after one. Only in specifier
    position: followed by a name, by `{ ; : =`, or by another such macro; never after struct/class/return/…,
    never on a preprocessor line."""
    out = bytearray(src)
    for line in re.finditer(rb'(?m)^(?!\s*#).*$', src):
        text = line.group(0)
        for m in re.finditer(rb'\b([A-Z][A-Z0-9_]{2,})\b', text):
            before = text[:m.start()].rstrip()
            prev = re.search(rb'(\w+)$', before)
            if prev and prev.group(1) in NOT_SPEC:
                continue
            after = text[m.end():]
            # ObjC: an ALL-CAPS typedef as a C function's return type (`static BOOL f(`, `SEL g(`) — no ctors in ObjC
            if keep_ret_type and re.fullmatch(rb'((static|inline|extern|__inline__)\s*)*', before) \
                    and re.match(rb'\s*\**\s*[A-Za-z_]\w*\s*\(', after):
                continue
            name_follows = re.match(rb'\s+[A-Za-z_~]', after) is not None
            body_follows = re.match(rb'\s*[{;:=]', after) is not None and (before.endswith(b')') or before.endswith(b'const')
                                                                            or before.endswith(b'noexcept') or before == b'')
            if name_follows and not after.lstrip().startswith((b'=', b'(')) or body_follows:
                if name_follows and before.endswith((b'(', b',', b'=', b'<', b'return')):
                    continue                     # an argument / value, not a specifier
                s, e = line.start() + m.start(), line.start() + m.end()
                out[s:e] = b' ' * (e - s)
        # trailing attribute macro WITH arguments, between a declaration and its `;` / `{`:
        # `- (void)f:(id)x AF_API_AVAILABLE(ios(10), macosx(10.12));`, `int g(void) NS_AVAILABLE_IOS(8_0) {`.
        # Only after `)` / a name / `*` / `]` on the same line (never a call statement, assignment or argument).
        for m in re.finditer(rb'\b([A-Z][A-Z0-9_]{2,})\s*\(', text):
            before = text[:m.start()].rstrip()
            prev = re.search(rb'(\w+)$', before)
            if not before or not re.search(rb'[\w)*\]]$', before) or (prev and prev.group(1) in NOT_SPEC) \
                    or re.match(rb'\s*[}]?\s*(if|while|for|switch|else|do)\b', text):   # `if (x) FOO(y);` is a call
                continue
            depth, j = 0, m.end() - 1
            while j < len(text):
                depth += (text[j:j + 1] == b'(') - (text[j:j + 1] == b')')
                if depth == 0:
                    break
                j += 1
            if depth != 0 or not re.match(rb'\s*([;{]|$)', text[j + 1:]):
                continue
            s, e = line.start() + m.start(), line.start() + j + 1
            out[s:e] = b' ' * (e - s)
    return bytes(out)


def objc_prep(src):
    return c_prep(src, keep_ret_type=True)


MACRO_LINE = re.compile(rb'(?m)^([ \t]*)([A-Z][A-Z0-9_]{2,})([ \t]*\((?:[^()\n]|\((?:[^()\n]|\([^()\n]*\))*\))*\))?[ \t]*(//.*)?$')


def cpp_prep(src):
    """c_prep, then (C++ only) blank a line that is nothing but an ALL-CAPS macro, bare or with arguments
    (`NLOHMANN_JSON_NAMESPACE_BEGIN`, `JSON_HEDLEY_NON_NULL(3)`, `JSON_HEDLEY_DEPRECATED_FOR(3.0.0, f())`): tree-sitter
    reads it as the start of a declaration and swallows the next function. Kept when the line continues a macro
    (`\\` before), or when a `{` / `:` follows (a macro that opens a body, e.g. `TEST_CASE("x")` then `{`)."""
    out = bytearray(c_prep(src))
    for m in MACRO_LINE.finditer(src):
        if m.group(2) in NOT_SPEC:
            continue
        before = src[:m.start()].rstrip(b' \t\r\n')
        if before.endswith(b'\\'):
            continue
        nxt = re.match(rb'\s*(\S)', src[m.end():])
        if nxt is None or nxt.group(1) in (b'{', b':', b';', b')', b','):
            continue
        s, e = m.start(2), (m.end(3) if m.group(3) else m.end(2))
        out[s:e] = b' ' * (e - s)
    _blank_split_conditionals(src, out)
    _cpp_fix_try_decltype_defines(out, src)
    return bytes(out)


def _cpp_fix_try_decltype_defines(out, orig):
    """C++ only, lines/columns kept. (1) `JSON_TRY` / `JSON_CATCH(...)` (an ALL-CAPS `*_TRY` alone on a line, an
    `*_CATCH(` opening a line) become `try` / `catch(`: as macros they swallow the function that follows. (2) a comma
    expression in `decltype(a, b)` (SFINAE trailing return) keeps only its last operand: tree-sitter rejects the comma.
    (3) a `/* */` comment on a `#define` continuation line is blanked: before the `\\` it ends the macro early."""
    src = bytes(out)
    for m in re.finditer(rb'(?m)^[ \t]*([A-Z][A-Z0-9_]*_TRY)(?=[ \t]*(\{|$))', orig):
        if re.match(rb'\s*\{', orig[m.end():]):
            out[m.start(1):m.end(1)] = b'try'.ljust(m.end(1) - m.start(1))
    for m in re.finditer(rb'(?m)^[ \t]*([A-Z][A-Z0-9_]*_CATCH)[ \t]*\(', orig):
        out[m.start(1):m.end(1)] = b'catch'.ljust(m.end(1) - m.start(1))
    for m in re.finditer(rb'\bdecltype\s*\(', src):
        depth, j, last = 1, m.end(), None
        while j < len(src) and depth:
            c = src[j:j + 1]
            if c in b'([{':
                depth += 1
            elif c in b')]}':
                depth -= 1
            elif c == b',' and depth == 1:
                last = j
            elif c == b';':
                break
            j += 1
        if depth == 0 and last is not None:
            out[m.end():last + 1] = re.sub(rb'[^\n]', b' ', bytes(out[m.end():last + 1]))
    for m in re.finditer(rb'(?m)^[ \t]*#[ \t]*define\b(?:.*\\\r?\n)+.*$', src):
        for c in re.finditer(rb'/\*[^\n]*?\*/', src[m.start():m.end()]):
            out[m.start() + c.start():m.start() + c.end()] = b' ' * (c.end() - c.start())


def _blank_split_conditionals(src, out):
    """An innermost `#if … [#else …] #endif` whose first branch ends mid-declaration (a header without its body, a
    member-initialiser fragment: last code char not `; { }`) splits one construct across branches, which tree-sitter
    cannot parse. Keep the first branch, blank the directives and the alternative branches (lines/columns kept)."""
    lines = list(re.finditer(rb'(?m)^.*$', src))
    stack = []                                   # per open #if: [directive line indexes, branch-1 end idx, nested?]
    for i, ln in enumerate(lines):
        d = re.match(rb'\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b', ln.group(0))
        if not d:
            continue
        k = d.group(1)
        if k.startswith(b'if'):
            if stack:
                stack[-1][2] = True
            stack.append([[i], None, False])
        elif not stack:
            continue
        elif k in (b'elif', b'else'):
            if stack[-1][1] is None:
                stack[-1][1] = i
            stack[-1][0].append(i)
        else:
            first, b1end, nested = stack.pop()
            if nested:
                if stack:
                    stack[-1][2] = True
                continue
            dirs = first + [i]                   # directive line indexes: #if, #elif/#else…, #endif
            branches = [(dirs[k] + 1, dirs[k + 1]) for k in range(len(dirs) - 1)]
            code = [_code(b'\n'.join(lines[j].group(0) for j in range(a, b))) for a, b in branches]
            if any(b'#' in c for c in code) or not code[0]:
                continue
            prev = next((_code(bytes(out[lines[j].start():lines[j].end()])) for j in range(dirs[0] - 1, -1, -1)
                         if _code(bytes(out[lines[j].start():lines[j].end()]))), b';')
            head_open = prev[-1:] not in (b';', b'{', b'}', b':') and not prev.startswith(b'#')
            if head_open:                        # header before #if, its body / `= delete;` inside: keep a body
                keep = next((k for k, c in enumerate(code) if b'{' in c), 0 if len(code) == 1 and not re.search(rb'[{};]', code[0]) else None)
            elif code[0][-1:] not in (b';', b'{', b'}'):
                keep = 0                         # first branch ends mid-declaration
            else:
                continue
            if keep is None:
                continue
            for k, (a, b) in enumerate(branches):
                for j in ([dirs[k]] + ([] if k == keep else list(range(a, b)))):
                    x, y = lines[j].start(), lines[j].end()
                    out[x:y] = re.sub(rb'[^\s]', b' ', bytes(out[x:y]))
            x, y = lines[i].start(), lines[i].end()
            out[x:y] = re.sub(rb'[^\s]', b' ', bytes(out[x:y]))


def _code(b):
    return re.sub(rb'//[^\n]*|/\*.*?\*/', b'', b, flags=re.S).strip()


def kotlin_prep(src):
    """Two Kotlin forms the grammar misreads, blanked with spaces (lines and columns kept):
    an annotation WITH arguments on the same line as a declaration (`@JvmName("x") fun f() {}` -> unnamed lambda),
    and a trailing comma before `)` (`class A(\n  val x: Int,\n) {` -> the class body becomes a lambda)."""
    out = bytearray(src)
    for m in re.finditer(rb'@[A-Za-z_][\w.:]*\((?:[^()"\n]|"[^"\n]*")*\)(?=[ \t]+(?:[a-z]+[ \t]+)*(?:fun|val|var|class|object|constructor)\b)', src):
        out[m.start():m.end()] = b' ' * (m.end() - m.start())
    for m in re.finditer(rb',(?=\s*\))', src):
        out[m.start()] = 32
    return bytes(out)


def powershell_prep(src):
    """Grammar bug: a variable followed directly by `#` INSIDE a double-quoted string ("$Sign#,##0.00") is read as a
    comment and breaks the whole function. Blank that `#` (same length) only when the line has an open `"` before it."""
    out = bytearray(src)
    for line in re.finditer(rb'(?m)^.*$', src):
        text = line.group(0)
        for m in re.finditer(rb'\$\w+(#)', text):
            if text[:m.start()].count(b'"') % 2 == 1:
                out[line.start() + m.start(1)] = 32
    # Grammar gap: an EMPTY script block (`$x = {}`, `param($On = {})`, a bare `{}` value) is a parse error that
    # swallows the enclosing function. Give it an empty statement (`{;}`) — line numbers are unchanged.
    return re.sub(rb'(?<![@$])\{([ \t]*)\}', rb'{;\1}', bytes(out))


# --- Dart (reference: package:analyzer; constructors = Class or Class.name, getters/setters/operators named) ---

DART_SIGS = {'function_signature', 'getter_signature', 'setter_signature', 'operator_signature',
             'constructor_signature', 'factory_constructor_signature', 'constant_constructor_signature',
             'redirecting_factory_constructor_signature'}


def _dart_name(sig):
    t = sig.type
    if t in ('constructor_signature', 'factory_constructor_signature', 'constant_constructor_signature',
             'redirecting_factory_constructor_signature'):
        ids = []
        for c in sig.children:
            if c.type in ('formal_parameter_list', 'parameters', 'initializers', 'redirection'):
                break
            if c.type == 'identifier':
                ids.append(_t(c))
        return '.'.join(ids) or None
    if t == 'operator_signature':
        m = re.search(r'operator\s*([^\s(]+)', _t(sig))
        return m.group(1) if m else None
    nm = _f(sig, 'name') or _kid(sig, 'identifier')
    return _t(nm) or None


def classify_dart(n):
    t = n.type
    if t == 'method_signature':
        inner = next((c for c in n.named_children if c.type in DART_SIGS), None)
        return ('fn', _dart_name(inner), ()) if inner is not None else None
    if t in DART_SIGS and (n.parent is None or n.parent.type != 'method_signature'):
        return ('fn', _dart_name(n), ())
    if t == 'function_expression':
        return ('fn', None, ())
    if t in ('class_definition', 'mixin_declaration', 'extension_declaration', 'enum_declaration',
             'extension_type_declaration'):
        return ('scope', _scope_name(n))
    return None


def label_dart(n, scope):
    """Dart calls are `target` + `selector(argument_part)`: callee = the identifier before the selector."""
    p = n.parent
    if p is not None and p.type in ('argument', 'named_argument'):
        p = p.parent
    if p is None or p.type != 'arguments':
        return None
    sel = p.parent.parent if p.parent is not None and p.parent.type == 'argument_part' else None
    if sel is None or sel.type != 'selector':
        return None
    prev = sel.prev_named_sibling
    name = _last_ident(_t(prev)) if prev is not None else ''
    s = None
    for a in p.named_children:
        x = a.named_children[0] if a.type == 'argument' and a.named_children else a
        if x.type == 'string_literal':
            s = _unq(_t(x))[:60]
            break
    if name.lower() in ROUTE_VERBS and s and s.startswith('/'):
        return f'{name.upper()} {s} handler'
    return f"{name or 'callback'}('{s}')" if s is not None else (name or 'callback')


def sig_dart(n):
    ps = _desc(n, ('formal_parameter_list',))
    return len([c for c in ps.named_children if c.type != 'comment']) if ps is not None else 0


# --- Zig (reference: std.zig.Ast fn_decl = a function with a body; Zig has no lambdas) ---

def classify_zig(n):
    t = n.type
    if t == 'Decl':
        proto, body = _kid(n, 'FnProto'), _kid(n, 'Block')
        if proto is not None and body is not None:
            nm = _f(proto, 'function') or _kid(proto, 'IDENTIFIER')
            return ('fn', _t(nm) or None, ())
        return None
    if t == 'VarDecl' and _desc(n, ('ContainerDecl',), skip=('Block', 'FnProto')) is not None:
        nm = _f(n, 'variable_type_function') or _kid(n, 'IDENTIFIER')
        return ('scope', _t(nm) or None)
    if t == 'TestDecl':
        s = _kid(n, 'STRINGLITERALSINGLE', 'IDENTIFIER')
        return ('scope', f'test {_t(s)}' if s is not None else 'test')
    return None


def sig_zig(n):
    proto = _kid(n, 'FnProto')
    ps = _kid(proto, 'ParamDeclList') if proto is not None else None
    return len([c for c in ps.named_children if c.type == 'ParamDecl']) if ps is not None else 0


# --- Verilog / SystemVerilog (reference: slang; function / task with a body, class `new`; always blocks unnamed) ---

V_SCOPES = {'module_declaration', 'class_declaration', 'interface_declaration', 'package_declaration',
            'program_declaration', 'checker_declaration'}


def _v_name(n, ident_types):
    x = _desc(n, ident_types, skip=('function_statement_or_null', 'statement_or_null', 'tf_item_declaration',
                                    'block_item_declaration', 'tf_port_list'))
    if x is None:
        return None
    return _last_ident(_t(x).split('::')[-1]) or None


def _v_header(n):
    """name and class of `function [type] [C::]name(` — read from the header text, because tree-sitter-verilog
    turns an out-of-class `C::name` into an ERROR node."""
    head = re.split(r'[(;]', _t(n), 1)[0]
    m = re.search(r'(\w+)\s*::\s*(\w+)\s*$', head)
    if m:
        return m.group(2), (m.group(1),)
    ids = IDENT.findall(head)
    return (ids[-1] if ids else None), ()


def classify_verilog(n):
    t = n.type
    if t in ('function_declaration', 'task_declaration'):
        name, extra = _v_header(n)
        return ('fn', name, extra)
    if t == 'class_constructor_declaration':
        return ('fn', 'new', ())
    if t == 'always_construct':
        return ('fn', None, ())
    if t in V_SCOPES:
        x = _desc(n, ('simple_identifier', 'class_identifier', 'module_identifier', 'interface_identifier',
                      'package_identifier', 'program_identifier'), skip=('module_or_generate_item', 'class_item',
                                                                          'list_of_port_declarations'))
        return ('scope', _last_ident(_t(x)) if x is not None else None)
    return None


def label_verilog(n, scope):
    if n.type == 'always_construct':
        kw = _kid(n, 'always_keyword')
        return _t(kw) or 'always'
    return None


V_HEAD = re.compile(r'^\s*(?:(?:virtual|static|protected|local|automatic|pure|extern|forkjoin)\s+)*(function|task)\b'
                    r'(?:\s+(?:automatic|static))?([^;(]*?)(\w+)\s*(?:::\s*(\w+))?\s*[(;]', re.M)


def verilog_post(src, rel, funcs, line_offset=0):
    """tree-sitter-verilog loses whole regions of class-heavy SystemVerilog (ERROR nodes). Fallback: every
    `function|task … name(` header in the text whose name has no module row starting on that line is added,
    ended at its endfunction / endtask. `extern` / `pure virtual` prototypes (no body) are skipped.
    Only headers on lines inside a tree-sitter ERROR / MISSING region are added: where the grammar parsed the code,
    its answer stands and the text rule adds nothing (on pulp-platform/axi all 63 fallback rows are inside ERROR)."""
    err_lines = set()
    todo = [get_parser('verilog').parse(src).root_node]
    while todo:
        n = todo.pop()
        if n.type == 'ERROR' or n.is_missing:
            err_lines.update(range(n.start_point[0] + 1 + line_offset, n.end_point[0] + 2 + line_offset))
        else:
            todo.extend(n.children)
    if not err_lines:
        return funcs
    text = src.decode('utf-8', 'replace')
    # comments out (same length, newlines kept) so commented-out functions never become rows
    text = re.sub(r'/\*.*?\*/', lambda m: re.sub(r'[^\n]', ' ', m.group(0)), text, flags=re.S)
    text = re.sub(r'//[^\n]*', lambda m: ' ' * len(m.group(0)), text)
    have = {(f['name'], f['line_start']) for f in funcs if f['named']}
    starts = [0] + [m.end() for m in re.finditer('\n', text)]
    import bisect
    for m in V_HEAD.finditer(text):
        head = m.group(0)
        if re.search(r'\b(extern|pure)\b', head):
            continue
        name, cls = (m.group(4), m.group(3)) if m.group(4) else (m.group(3), None)
        line = bisect.bisect_right(starts, m.start() + len(head) - len(head.lstrip())) + line_offset
        if (name, line) in have or line not in err_lines:
            continue
        end = re.compile(r'\bend' + m.group(1) + r'\b').search(text, m.end())
        if end is None:
            continue
        e = bisect.bisect_right(starts, end.start()) + line_offset
        funcs.append({'file': rel, 'name': name, 'line_start': line, 'line_end': e, 'named': True,
                      'scope': cls or '', 'label': name, 'arity': 0})
    return funcs


def sig_verilog(n):
    return len([1 for c in _iter_desc(n, stop=('function_statement_or_null', 'statement_or_null', 'seq_block'))
                if c.type in ('tf_port_item', 'tf_port_item1', 'tf_port_declaration')])


def _iter_desc(n, stop=()):
    todo = list(n.named_children)
    while todo:
        c = todo.pop()
        yield c
        if c.type not in stop:
            todo.extend(c.named_children)


# --- Elixir (reference: Code.string_to_quoted; every def/defp/defmacro/defmacrop CLAUSE is one function; fn unnamed) ---

EX_DEFS = {'def', 'defp', 'defmacro', 'defmacrop'}
EX_SCOPES = {'defmodule', 'defprotocol', 'defimpl', 'describe', 'test'}


def _ex_head(n):
    args = _kid(n, 'arguments')
    return args.named_children[0] if args is not None and args.named_children else None


def classify_elixir(n):
    t = n.type
    if t == 'call':
        target = _f(n, 'target')
        kw = _t(target) if target is not None and target.type == 'identifier' else ''
        if kw in EX_DEFS:
            head = _ex_head(n)
            while head is not None and head.type == 'binary_operator' and _t(_f(head, 'operator')) == 'when':
                head = _f(head, 'left')
            if head is None:
                return None
            if head.type == 'call':
                tg = _f(head, 'target')
                name = _t(tg) if tg is not None and tg.type == 'identifier' else None
            elif head.type == 'identifier':
                name = _t(head)
            else:                                   # unquote(...) or an operator head: no source name
                name = None
            return ('fn', name, ()) if name else None
        if kw in EX_SCOPES:
            head = _ex_head(n)
            label = _unq(_t(head)) if head is not None else kw
            return ('scope', label if kw in ('defmodule', 'defprotocol', 'defimpl') else f"{kw}('{label[:60]}')")
        return None
    if t == 'anonymous_function':
        return ('fn', None, ())
    return None


def sig_elixir(n):
    if n.type != 'call':
        return generic_sig(n)
    head = _ex_head(n)
    while head is not None and head.type == 'binary_operator':
        head = _f(head, 'left')
    a = _kid(head, 'arguments') if head is not None else None
    return len(a.named_children) if a is not None else 0


# --- Lua / Luau (reference: luaparse / full_moon; a function value assigned to a name or an identifier key is named) ---

def classify_lua(n):
    t = n.type
    if t == 'function_declaration':
        nm = _f(n, 'name')
        if nm is None:
            return ('fn', None, ())
        if nm.type == 'dot_index_expression':
            return ('fn', _t(_f(nm, 'field')), (_t(_f(nm, 'table')),))
        if nm.type == 'method_index_expression':
            return ('fn', _t(_f(nm, 'method')), (_t(_f(nm, 'table')),))
        return ('fn', _t(nm), ())
    if t == 'function_definition':
        return ('fn', None, ())
    if t == 'field' and getattr(_f(n, 'value'), 'type', '') == 'table_constructor':
        k = _f(n, 'name')
        return ('scope', _t(k)) if k is not None and k.type == 'identifier' else None
    return None


# --- Go (reference: go/parser FuncDecl; FuncLit unnamed) ---

def classify_go(n):
    t = n.type
    if t == 'function_declaration':
        return ('fn', _t(_f(n, 'name')) or None, ())
    if t == 'method_declaration':
        recv = _f(n, 'receiver')
        rt = _desc(recv, ('type_identifier',)) if recv is not None else None
        return ('fn', _t(_f(n, 'name')) or None, (_t(rt),) if rt is not None else ())
    if t == 'func_literal':
        return ('fn', None, ())
    return None


# --- Rust (reference: syn; fn / impl fn / trait fn WITH a body; closures unnamed) ---

def classify_rust(n):
    t = n.type
    if t == 'function_item':
        return ('fn', _t(_f(n, 'name')) or None, ())
    if t == 'closure_expression':
        return ('fn', None, ())
    if t == 'impl_item':
        ty, tr = _f(n, 'type'), _f(n, 'trait')
        name = _last_ident(re.sub(r'<.*', '', _t(ty))) if ty is not None else None
        if tr is not None and name:
            name = f'{name}<{_last_ident(re.sub(r"<.*", "", _t(tr)))}>'
        return ('scope', name)
    if t in ('trait_item', 'mod_item'):
        return ('scope', _scope_name(n))
    return None


# --- Kotlin (reference: Kotlin PSI; named functions + secondary constructors named after the class) ---

def classify_kotlin(n):
    t = n.type
    if t == 'function_declaration':
        nm = _kid(n, 'simple_identifier')
        return ('fn', (_t(nm) or '').strip('`') or None, ())  # PSI names `quoted names` without backticks
    if t == 'secondary_constructor':
        cls = n.parent.parent if n.parent is not None else None
        nm = _kid(cls, 'type_identifier') if cls is not None else None
        return ('fn', _t(nm) if nm is not None else 'constructor', ())
    if t in ('lambda_literal', 'anonymous_function'):
        return ('fn', None, ())
    if t in ('class_declaration', 'object_declaration'):
        return ('scope', _t(_kid(n, 'type_identifier', 'simple_identifier')) or None)
    if t == 'companion_object':
        return ('scope', _t(_kid(n, 'type_identifier')) or 'Companion')
    return None


def sig_kotlin(n):
    ps = _kid(n, 'function_value_parameters')
    if ps is None:
        return generic_sig(n)
    return ','.join(re.sub(r'\s+', '', _t(_kid(p, 'user_type', 'nullable_type', 'function_type')) or '?')
                    for p in ps.named_children if p.type == 'parameter')


# --- Scala (reference: scalameta Defn.Def = def with a body; abstract defs not counted; x => … unnamed) ---

def classify_scala(n):
    t = n.type
    if t == 'function_definition':
        nm = _t(_f(n, 'name'))
        if nm and len(nm) > 2 and nm[0] == '`' and nm[-1] == '`':   # backquoted identifier: scalameta reports the bare name
            nm = nm[1:-1]
        return ('fn', nm or None, ())
    if t == 'lambda_expression':
        return ('fn', None, ())
    if t in ('class_definition', 'object_definition', 'trait_definition', 'enum_definition'):
        return ('scope', _scope_name(n))
    return None


def sig_scala(n):
    groups = [c for c in n.named_children if c.type == 'parameters']
    return '|'.join(','.join(re.sub(r'\s+', '', _t(_f(p, 'type'))) for p in g.named_children if p.type == 'parameter')
                    for g in groups)


# --- Swift (reference: swiftc -dump-parse; func / init / deinit; closures unnamed) ---

def classify_swift(n):
    t = n.type
    if t in ('function_declaration', 'protocol_function_declaration'):   # protocol requirements count too (swiftc lists them)
        return ('fn', _t(_f(n, 'name')) or None, ())
    if t == 'init_declaration':
        return ('fn', 'init', ())
    if t == 'deinit_declaration':
        return ('fn', 'deinit', ())
    if t == 'subscript_declaration':
        return ('fn', 'subscript', ())
    if t == 'lambda_literal':
        # `defer { … }` parses as a call to `defer` with a trailing closure; swiftc names that closure `$defer`
        call = n.parent.parent if n.parent is not None and n.parent.type == 'call_suffix' else None
        if call is not None and call.type == 'call_expression' and call.named_children and _t(call.named_children[0]) == 'defer':
            return ('fn', '$defer', ())
        return ('fn', None, ())
    if t in ('class_declaration', 'protocol_declaration'):
        nm = _f(n, 'name')
        return ('scope', _last_ident(_t(nm)) if nm is not None else None)
    return None


def start_swift(n):
    """swiftc starts a declaration at its keyword (`func` / `init`), after `@attributes` and modifiers."""
    for c in n.children:
        if c.type not in ('modifiers', 'attribute', 'comment', 'multiline_comment'):
            return c.start_point[0]
    return n.start_point[0]


def sig_swift(n):
    ps = [c for c in n.named_children if c.type == 'parameter']
    out = []
    for p in ps:
        ext = _f(p, 'external_name')
        out.append(_t(ext) if ext is not None else _t(_f(p, 'name')))
    return ':'.join(out)


# --- Ruby (reference: RubyVM AST; def / def self.x; blocks and lambdas unnamed) ---

def classify_ruby(n):
    t = n.type
    if t in ('method', 'singleton_method'):
        return ('fn', _t(_f(n, 'name')) or None, ('self',) if t == 'singleton_method' else ())
    if t in ('block', 'do_block') and n.parent is not None and n.parent.type == 'lambda':
        return None                              # the body of ->(x) { … }: part of that lambda
    if t in ('block', 'do_block', 'lambda'):
        return ('fn', None, ())
    if t in ('class', 'module', 'singleton_class'):
        return ('scope', _scope_name(n) or 'self')
    return None


# --- PHP (reference: nikic/php-parser; Function_ + ClassMethod; closures / arrow fns unnamed) ---

def classify_php(n):
    t = n.type
    if t in ('function_definition', 'method_declaration'):
        return ('fn', _t(_f(n, 'name')) or None, ())
    if t in ('anonymous_function', 'anonymous_function_creation_expression', 'arrow_function'):
        return ('fn', None, ())
    if t in ('class_declaration', 'interface_declaration', 'trait_declaration', 'enum_declaration'):
        return ('scope', _scope_name(n))
    return None


# --- Perl (reference: PPI; named sub with a body, name as written; anonymous sub unnamed) ---

def classify_perl(n):
    t = n.type
    if t in ('subroutine_declaration_statement', 'method_declaration_statement'):
        if _f(n, 'body') is None:
            return None
        return ('fn', _t(_f(n, 'name')) or None, ())
    if t == 'anonymous_subroutine_expression':
        return ('fn', None, ())
    if t == 'phaser_statement':                   # BEGIN { } / END { }: PPI counts these as named subs
        return ('fn', _t(n).split(None, 1)[0].split('{')[0] or None, ())
    return None


# --- Bash (reference: mvdan/sh FuncDecl; no anonymous functions) ---

def classify_bash(n):
    if n.type == 'function_definition':
        return ('fn', _t(_f(n, 'name')) or None, ())
    return None


# --- R (reference: R parse(); name <- function / name = function; others unnamed) ---

def classify_r(n):
    if n.type == 'function_definition':
        return ('fn', None, ())                  # named by value_name (binary_operator <- / = / ->)
    return None


# --- Julia (reference: JuliaSyntax; function f / short form f(x) = …; macros, x -> … unnamed) ---

def _jl_callee(sig):
    """Name defined by a signature: f(x), Base.f(x), f{T}(x), f(x)::T, f(x) where T, (x::T)(…) -> None."""
    x = sig
    while x is not None and x.type in ('where_expression', 'typed_expression', 'signature', 'parenthesized_expression'):
        x = x.named_children[0] if x.named_children else None
    if x is None:
        return None
    if x.type == 'call_expression':
        f = x.named_children[0] if x.named_children else None
        if f is None:
            return None
        if f.type == 'parametrized_type_expression':
            f = f.named_children[0]
        if f.type in ('identifier', 'operator'):
            return _t(f)
        if f.type == 'field_expression':
            tail = _t(f).split('.')[-1].strip()
            if tail.startswith(':'):                                # Base.:<(…) quoted operator
                return tail[1:].strip('()') or None
            return _last_ident(_t(f)) or tail
        if f.type == 'parenthesized_expression':                    # functor: (m::T{…})(x) -> T
            m = re.match(r'\(\s*\w*\s*::\s*\$?([\w.$]+)', _t(f))
            if m:
                return m.group(1).split('.')[-1] or None
        return None
    if x.type in ('binary_expression', 'unary_expression'):          # operator method: a + b = …
        op = _kid(x, 'operator')
        return _t(op) if op is not None else None
    return None


def classify_julia(n):
    t = n.type
    if t == 'function_definition':
        sig = _kid(n, 'signature')
        if sig is None:
            return ('fn', None, ())
        return ('fn', _jl_callee(sig), ())
    if t == 'assignment':
        lhs = n.named_children[0] if n.named_children else None
        x = lhs
        while x is not None and x.type in ('where_expression', 'typed_expression'):
            x = x.named_children[0] if x.named_children else None
        if x is not None and x.type == 'call_expression':
            return ('fn', _jl_callee(lhs), ())
        return None
    if t in ('arrow_function_expression', 'macro_definition', 'do_clause'):
        return ('fn', None, ())
    if t in ('module_definition', 'struct_definition'):
        return ('scope', _t(_kid(n, 'identifier')) or None)
    return None


def sig_julia(n):
    call = _desc(n, ('call_expression',))
    a = _kid(call, 'argument_list') if call is not None else None
    return len(a.named_children) if a is not None else 0


# --- GDScript (reference: gdtoolkit; func / static func / _init; lambdas unnamed) ---

def start_gdscript(n):
    """gdtoolkit starts a func at its `func`/`static` keyword, after leading `@annotations`."""
    for c in n.children:
        if c.type not in ('annotations', 'annotation', 'comment'):
            return c.start_point[0]
    return n.start_point[0]


def classify_gdscript(n):
    t = n.type
    if t == 'function_definition':
        return ('fn', _t(_f(n, 'name')) or None, ())
    if t == 'constructor_definition':
        return ('fn', '_init', ())
    if t == 'lambda':
        return ('fn', None, ())
    if t == 'class_definition':
        return ('scope', _scope_name(n))
    return None


# --- PowerShell (reference: PowerShell parser; functions (scope prefix stripped) + class methods; { } values unnamed) ---

def classify_powershell(n):
    t = n.type
    if t == 'function_statement':
        nm = _kid(n, 'function_name')
        return ('fn', re.sub(r'^(script|global|local|private):', '', _t(nm), flags=re.I) or None, ())
    if t == 'class_method_definition':
        nm = _kid(n, 'simple_name')
        return ('fn', _t(nm) or None, ())
    if t == 'script_block_expression':
        return ('fn', None, ())
    if t == 'class_statement':
        return ('scope', _t(_kid(n, 'simple_name')) or None)
    return None


# --- Solidity (reference: solc; FunctionDefinition incl. constructor / fallback / receive; modifiers unnamed) ---

def classify_solidity(n):
    t = n.type
    if t == 'function_definition':
        return ('fn', _t(_f(n, 'name')) or None, ())
    if t == 'constructor_definition':
        return ('fn', 'constructor', ())
    if t == 'fallback_receive_definition':
        return ('fn', 'receive' if _t(n).lstrip().startswith('receive') else 'fallback', ())
    if t == 'modifier_definition':
        return ('fn', None, ())                 # the reference lists modifiers as unnamed
    if t in ('contract_declaration', 'interface_declaration', 'library_declaration'):
        return ('scope', _scope_name(n))
    return None


def label_solidity(n, scope):
    return f"modifier {_t(_f(n, 'name'))}" if n.type == 'modifier_definition' else None


# --- C# (reference: Roslyn; methods, ctors, dtors, operators, local functions; lambdas / delegates unnamed) ---

def classify_csharp(n):
    t = n.type
    if t in ('method_declaration', 'constructor_declaration', 'local_function_statement'):
        return ('fn', _t(_f(n, 'name')) or None, ())
    if t == 'destructor_declaration':
        return ('fn', _t(_f(n, 'name')) or None, ())   # Roslyn: Identifier without the ~
    if t == 'operator_declaration':
        op = _f(n, 'operator')
        if op is None:
            m = re.search(r'operator\s*([^\s(]+)', _t(n))
            return ('fn', f'operator {m.group(1)}' if m else None, ())
        return ('fn', f'operator {_t(op)}', ())
    if t == 'conversion_operator_declaration':
        return ('fn', f"operator {re.sub(chr(32) + '+', ' ', _t(_f(n, 'type')))}", ())
    if t in ('lambda_expression', 'anonymous_method_expression'):
        return ('fn', None, ())
    if t in ('class_declaration', 'struct_declaration', 'interface_declaration', 'record_declaration',
             'namespace_declaration', 'file_scoped_namespace_declaration'):
        return ('scope', _scope_name(n))
    return None


# --- Nix (reference: rnix-parser; a binding whose value is a lambda is named (curried = one); others unnamed) ---

def classify_nix(n):
    t = n.type
    if t == 'function_expression':
        if n.parent is not None and n.parent.type == 'function_expression':
            return None                          # curried `f = a: b: …`: the inner lambda is part of f
        return ('fn', None, ())                  # named by value_name (binding attrpath)
    if t == 'binding' and getattr(_f(n, 'expression'), 'type', '') in ('attrset_expression', 'rec_attrset_expression'):
        return ('scope', _last_ident(_t(_f(n, 'attrpath'))))
    return None


# --- HCL / Terraform (reference: hclsyntax; top-level blocks `type.label…`, locals attributes `local.x`) ---

def hcl_functions(src, lang, rel, line_offset=0):
    tree = get_parser('hcl').parse(src)
    body = _kid(tree.root_node, 'body')
    out = []
    for b in (body.named_children if body is not None else []):
        if b.type != 'block':
            continue
        kind = _t(_kid(b, 'identifier'))
        labels = [_unq(_t(c)) for c in b.named_children if c.type in ('string_lit', 'identifier')][1:]
        if kind == 'locals':
            inner = _kid(b, 'body')
            for a in (inner.named_children if inner is not None else []):
                if a.type == 'attribute':
                    out.append(_row(rel, 'local.' + _t(_kid(a, 'identifier')), a, kind, line_offset))
            continue
        name = 'var.' + '.'.join(labels) if kind == 'variable' else '.'.join([kind] + labels)
        if kind == 'provider':
            inner = _kid(b, 'body')
            for a in (inner.named_children if inner is not None else []):
                if a.type == 'attribute' and _t(_kid(a, 'identifier')) == 'alias':
                    name += '.' + _unq(_t(a.named_children[-1]))
        out.append(_row(rel, name, b, '', line_offset))
    return out


def _row(rel, name, node, scope, line_offset=0, named=True, start=None, end=None, arity=0):
    s = start if start is not None else node.start_point[0] + 1 + line_offset
    e = end if end is not None else node.end_point[0] + 1 + line_offset
    return {'file': rel, 'name': name, 'line_start': s, 'line_end': e, 'named': named, 'scope': scope,
            'label': name, 'arity': arity}


SH_HEAD = re.compile(r'^([ \t]*)(?:function[ \t]+([\w:.@-]+)[ \t]*(?:\(\s*\))?|([\w:.@-]+)[ \t]*\(\s*\))[ \t]*\{', re.M)


def bash_post(src, rel, funcs, line_offset=0):
    """tree-sitter-bash can fall into an ERROR region (e.g. eval strings with escaped `${…}`) and flatten every later
    `name() {` header into loose tokens. Fallback: inside ERROR regions only, a line `name() {` / `function name {`
    becomes a row, ended at the first later line that is `}` at the header's indentation."""
    err_lines = set()
    todo = [get_parser('bash').parse(src).root_node]
    while todo:
        n = todo.pop()
        if n.type == 'ERROR' or n.is_missing:
            err_lines.update(range(n.start_point[0] + 1 + line_offset, n.end_point[0] + 2 + line_offset))
        else:
            todo.extend(n.children)
    if not err_lines:
        return funcs
    text = src.decode('utf-8', 'replace')
    lines = text.split('\n')
    have = {f['line_start'] for f in funcs if f['named']}
    starts = [0] + [m.end() for m in re.finditer('\n', text)]
    import bisect
    for m in SH_HEAD.finditer(text):
        name, ind = m.group(2) or m.group(3), m.group(1)
        i = bisect.bisect_right(starts, m.start()) - 1
        line = i + 1 + line_offset
        if line in have or line not in err_lines:
            continue
        close = re.compile(r'^' + re.escape(ind) + r'\}\s*(?:[;&|<>#].*)?$')
        e = next((j for j in range(i + 1, len(lines)) if close.match(lines[j])), None)
        if e is None:
            continue
        funcs.append({'file': rel, 'name': name, 'line_start': line, 'line_end': e + 1 + line_offset, 'named': True,
                      'scope': '', 'label': name, 'arity': 0})
    return funcs


TOP_MACRO_CALL = re.compile(rb'(?m)^([A-Za-z_]\w*)[ \t]*\(')


def c_only_prep(src):
    """C only: c_prep, then three repairs for constructs that make tree-sitter lose the following real functions.
    1. A top-level macro invocation without `;` (`rb_gen(static, tctx_, …)`, `CTL_RO_NL_GEN(version, …)`): a name at
       column 0 whose balanced `( … )` ends its line, preceded by a finished construct (`; } */`, blank, directive) and
       not followed by `{` or `;` — blanked (lines/columns kept).
    2. A comment inside a `#define` continuation (`/* … */ \\`): blanked."""
    out = bytearray(c_prep(src))
    for m in TOP_MACRO_CALL.finditer(src):
        if m.group(1) in NOT_SPEC or m.group(1) in (b'if', b'while', b'for', b'switch', b'sizeof'):
            continue
        tail = src[max(0, m.start() - 4000):m.start()].rstrip()
        last = tail[tail.rfind(b'\n') + 1:].lstrip()
        prev = _code(tail)
        if prev and prev[-1:] not in (b';', b'}') and not last.startswith(b'#'):
            continue
        depth, j = 0, m.end() - 1
        while j < len(src):
            c = src[j:j + 1]
            depth += (c == b'(') - (c == b')')
            if depth == 0 or c in (b';', b'{', b'}'):
                break
            j += 1
        if depth != 0 or src[j:j + 1] != b')' or not re.match(rb'[ \t]*(//[^\n]*)?\r?\n', src[j + 1:]):
            continue
        nxt = re.match(rb'\s*(\S)', src[j + 1:])
        if nxt is None or nxt.group(1) in (b'{', b';', b',', b')'):
            continue
        for k in range(m.start(), j + 1):
            if out[k] not in (10, 13):
                out[k] = 32
    for d in re.finditer(rb'(?m)^[ \t]*#[ \t]*define\b(?:[^\n]*\\\r?\n)+[^\n]*', src):
        for c in re.finditer(rb'/\*.*?\*/', d.group(0), re.S):
            s, e = d.start() + c.start(), d.start() + c.end()
            for k in range(s, e):
                if out[k] not in (10, 13):
                    out[k] = 32
    return bytes(out)

# ======== c8_table.py ========
# ---------- languages with no tree-sitter grammar in the pack (line rules), and embedded scripts ----------

def _lines(src):
    return src.decode('utf-8', 'replace').splitlines()


SQL_CREATE = re.compile(r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NON)?EDITIONABLE\s+)?(FUNCTION|PROCEDURE|TRIGGER)\s+'
                        r'([\w$#".]+)', re.I)
SQL_PKG_BODY = re.compile(r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NON)?EDITIONABLE\s+)?PACKAGE\s+BODY\b', re.I)
SQL_MEMBER = re.compile(r'^\s*(FUNCTION|PROCEDURE)\s+([\w$#"]+)', re.I)


def sql_functions(src, lang, rel, line_offset=0):
    """PL/SQL line rule (tree-sitter-sql cannot read PL/SQL): CREATE FUNCTION|PROCEDURE|TRIGGER, and FUNCTION /
    PROCEDURE members of a CREATE PACKAGE BODY that have a body (IS/AS before the next `;`)."""
    lines, out, in_body = _lines(src), [], False
    for i, ln in enumerate(lines, 1):
        m = SQL_CREATE.match(ln)
        if m:
            out.append(_row(rel, _unq(m.group(2).split('.')[-1]), None, '', start=i, end=i))
            in_body = False
            continue
        if SQL_PKG_BODY.match(ln):
            in_body = True
            continue
        m = SQL_MEMBER.match(ln) if in_body else None
        if m:
            head = ' '.join(lines[i - 1:i + 30])
            semi, isas = head.find(';'), re.search(r'\b(IS|AS)\b', head, re.I)
            if isas and (semi < 0 or isas.start() < semi):
                out.append(_row(rel, _unq(m.group(2)), None, '', start=i, end=i))
    return out


ROBOT_SECTION = re.compile(r'^\*+\s*(.+?)\s*\**\s*$')


def robot_functions(src, lang, rel, line_offset=0):
    """Robot Framework line rule: in a Keywords / Test Cases / Tasks section, every line that starts in column 1
    (not a comment, not `...`) is a keyword or test name. Runs until the next name or section."""
    out, section, cur = [], None, None
    lines = _lines(src)
    for i, ln in enumerate(lines, 1):
        m = ROBOT_SECTION.match(ln)
        if m:
            section = m.group(1).lower()
            cur = None
            continue
        if section and section.rstrip('s') in ('keyword', 'test case', 'task') and ln and (not ln[0].isspace() or (ln[0] == ' ' and len(ln) > 1 and not ln[1].isspace())) \
                and not ln.lstrip(' ').startswith(('#', '...')):
            # one leading space is not a separator in Robot (needs 2+ spaces / tab): still a name, kept as-is
            name = re.split(r'\s{2,}|\t', ln.rstrip())[0]
            cur = _row(rel, name, None, section.split()[0], start=i, end=i)
            out.append(cur)
        elif cur is not None and ln.strip():
            cur['line_end'] = i
    return out


def xslt_functions(src, lang, rel, line_offset=0):
    """XSLT with the stdlib's expat: xsl:template (@name, else `match=<pattern>` + ` mode=<mode>` — the tool's
    naming) and xsl:function (@name). Start = line of `<`, end = line of the closing tag."""
    import xml.parsers.expat as expat
    XSL = 'http://www.w3.org/1999/XSL/Transform'
    out, stack = [], []
    p = expat.ParserCreate(namespace_separator='|')
    norm = lambda s: ' '.join((s or '').split())

    def start(tag, attrs):
        ns, _, local = tag.rpartition('|')
        row = None
        if ns == XSL and local in ('template', 'function'):
            name, match, mode = norm(attrs.get('name')), norm(attrs.get('match')), norm(attrs.get('mode'))
            label = name or (f'match={match}' + (f' mode={mode}' if mode else '') if match else '')
            if label:
                row = _row(rel, label, None, '', start=p.CurrentLineNumber, end=p.CurrentLineNumber)
                out.append(row)
        stack.append(row)

    def end(tag):
        row = stack.pop()
        if row is not None:
            row['line_end'] = p.CurrentLineNumber

    p.StartElementHandler, p.EndElementHandler = start, end
    p.Parse(src, True)          # a parse error raises: listed in module_errors, never hidden
    return out


RES_LET = re.compile(r'^(\s*)(?:let|and)\s+(?:rec\s+)?([a-z_][\w\']*)\s*(?::(?:[^=]|=>)*)?=(?!>)\s*(?:async\s+)?'
                     r'(\(|fun\b|[a-z_][\w\']*\s*=>|%raw)')


def rescript_functions(src, lang, rel, line_offset=0):
    """ReScript line rule: `let [rec] name = (args) =>` / `let name = fun` / `and name = …` (also nested lets)."""
    out = []
    for i, ln in enumerate(_lines(src), 1):
        m = RES_LET.match(ln)
        if m and m.group(3) != '%raw':
            rest = ln[m.end(3) - 1:]
            if m.group(3) == '(' and '=>' not in ' '.join([rest]) and not ln.rstrip().endswith('('):
                continue                                # `let x = (a, b)` is a tuple, not a function
            out.append(_row(rel, m.group(2), None, '', start=i, end=i))
    return out


VB_MODS = r'(?:(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|NotOverridable|Overloads|Shadows|' \
          r'Static|Async|Iterator|Partial|Default|Narrowing|Widening|ReadOnly|WriteOnly|NotInheritable)\s+)*'
VB_START = re.compile(r'^\s*(?:<[^>]*>\s*)*' + VB_MODS + r'(Sub|Function|Operator)\s+([\w\[\]]+|[^\s(]+)', re.I)
VB_END = re.compile(r'^\s*End\s+(Sub|Function|Operator)\b', re.I)
VB_LAMBDA = re.compile(r'(?<![\w.])(Sub|Function)\s*\(', re.I)


def vbnet_functions(src, lang, rel, line_offset=0):
    """VB.NET line rule: a Sub / Function / Operator that has a matching `End …` is a method block (a MustOverride
    or Declare line has none); Sub New = `New`, operators = `Operator +`. Inline `Function(x) …` lambdas unnamed."""
    out, stack, lam = [], [], Counter()
    for i, ln in enumerate(_lines(src), 1):
        code = re.sub(r"'.*$", '', ln)
        m = VB_START.match(code)
        if m and not re.search(r'\b(MustOverride|Declare|Delegate)\b', code, re.I):
            kind, name = m.group(1).title(), re.sub(r'^\[(.*)\]$', r'\1', m.group(2))
            name = f'Operator {name}' if kind == 'Operator' else name
            stack.append((kind, name, i))
            continue
        e = VB_END.match(code)
        if e and stack:
            kind, name, s = stack.pop()
            out.append(_row(rel, name, None, '', start=s, end=i))
            continue
        for _ in VB_LAMBDA.finditer(code):
            lam[i] += 1
            out.append(_row(rel, f'lambda#{sum(lam.values())}', None, '', named=False, start=i, end=i))
    return out


def sfc_functions(src, lang, rel, line_offset=0):
    """Svelte / Vue: each <script> block's text is parsed with the JS or TS grammar (lang="ts") and walked with the
    JS rules; lines are shifted to the .svelte / .vue file. Template handlers are not read (the reference does not)."""
    doc = get_parser(lang).parse(src).root_node
    out = []
    for el in doc.named_children:
        if el.type != 'script_element':
            continue
        tag, raw = _kid(el, 'start_tag'), _kid(el, 'raw_text')
        if raw is None:
            continue
        ts = re.search(r'lang\s*=\s*["\']?(ts|typescript)', _t(tag) if tag is not None else '')
        out += legacy_functions(raw.text, 'typescript' if ts else 'javascript', rel, line_offset=raw.start_point[0])
    return out


def notebook_functions(src, lang, rel, line_offset=0):
    """.ipynb: code cells joined with "\\n" after dropping lines that start with % or ! — the tool's own numbering
    (parser.py _parse_notebook), so lines match the graph and the reference. Walked with the Python rules."""
    import json as _json
    nb = _json.loads(src)
    cells = nb.get('cells') or [c for ws in nb.get('worksheets', []) for c in ws.get('cells', [])]
    code = []
    for c in cells:
        if c.get('cell_type') != 'code':
            continue
        lines = c.get('source') or c.get('input') or []
        lines = lines.splitlines(keepends=True) if isinstance(lines, str) else list(lines)
        if not lines:
            continue
        first, py = lines[0].strip(), True
        if first == '%python' or first.startswith('%python '):
            lines = lines[1:]
        elif any(first == m or first.startswith(m + ' ') for m in ('%sql', '%r', '%scala', '%md', '%sh')):
            py = False                         # another language's cell: not in the Python text
        lines = [l for l in lines if not l.lstrip().startswith(('%', '!'))]
        if py and lines:                       # a cell left EMPTY is skipped, not joined as a blank line (SYN-F-568)
            code.append(''.join(lines))
    return legacy_functions('\n'.join(code).encode(), 'python', rel)


# ---------- the table: every survey language ----------
# crg = the tool's language labels whose files are scored (`c` too in a C++ / Obj-C repo: the tool labels .h as c)

def blank_bom(src):
    """A UTF-8 byte-order mark at the start hides the first declaration from some grammars (PowerShell: the whole
    first function). Blank it with 3 spaces so every byte offset and line stays the same."""
    return b'   ' + src[3:] if src.startswith(b'\xef\xbb\xbf') else src


def _r(grammar, classify, calls=(), sig=None, **kw):
    user = kw.pop('prep', None)                # every language: BOM blanked first, then its own prep (if any)
    kw['prep'] = (lambda s, u=user: u(blank_bom(s))) if user else blank_bom
    return dict(grammar=grammar, classify=classify, calls=set(calls), sig=sig, **kw)


CALLS_C = ('call_expression', 'message_expression')
RULES = {
    'c': _r('c', classify_c_only, CALLS_C, sig=lambda n: sig_c(n, typed=False), prep=c_only_prep),
    'cpp': _r('cpp', classify_c, CALLS_C, sig=sig_c, prep=cpp_prep),
    'objc': _r('objc', classify_c, CALLS_C, sig=sig_c, prep=objc_prep),
    'dart': _r('dart', classify_dart, (), sig=sig_dart, body_sibling={'function_body'}, label=label_dart),
    'zig': _r('zig', classify_zig, (), sig=sig_zig),
    'verilog': _r('verilog', classify_verilog, (), sig=sig_verilog, label=label_verilog, post=verilog_post),
    'elixir': _r('elixir', classify_elixir, ('call',), sig=sig_elixir),
    'lua': _r('lua', classify_lua, ('function_call',)),
    'luau': _r('luau', classify_lua, ('function_call',)),
    'go': _r('go', classify_go, ('call_expression',)),
    'rust': _r('rust', classify_rust, ('call_expression', 'macro_invocation')),
    'kotlin': _r('kotlin', classify_kotlin, ('call_expression',), sig=sig_kotlin, prep=kotlin_prep),
    'scala': _r('scala', classify_scala, ('call_expression',), sig=sig_scala),
    'swift': _r('swift', classify_swift, ('call_expression',), sig=sig_swift, start=start_swift),
    'ruby': _r('ruby', classify_ruby, ('call', 'method_call')),
    'php': _r('php', classify_php, ('function_call_expression', 'member_call_expression', 'scoped_call_expression')),
    'perl': _r('perl', classify_perl, ('function_call_expression', 'method_call_expression',
                                      'ambiguous_function_call_expression')),
    'bash': _r('bash', classify_bash, post=bash_post),
    'r': _r('r', classify_r, ('call',)),
    'julia': _r('julia', classify_julia, ('call_expression', 'macrocall_expression'), sig=sig_julia),
    'gdscript': _r('gdscript', classify_gdscript, ('call', 'attribute_call'), start=start_gdscript),
    'powershell': _r('powershell', classify_powershell, ('command', 'invokation_expression'), prep=powershell_prep),
    'solidity': _r('solidity', classify_solidity, ('call_expression',), label=label_solidity,
                   sig=lambda n: generic_sig(n, typed=True)),
    'csharp': _r('csharp', classify_csharp, ('invocation_expression', 'object_creation_expression'),
                 sig=lambda n: generic_sig(n, typed=True)),
    'nix': _r('nix', classify_nix, ('apply_expression',)),
}
LINE_RULES = {'hcl': hcl_functions, 'sql': sql_functions, 'robot': robot_functions, 'xslt': xslt_functions,
              'rescript': rescript_functions, 'vbnet': vbnet_functions, 'svelte': sfc_functions, 'vue': sfc_functions,
              'notebook': notebook_functions}
LEGACY = {'javascript', 'typescript', 'tsx', 'python', 'java'}           # treesitter-module.ipynb rules, unchanged
JS_GRAMMAR = {'.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
              '.ts': 'typescript', '.mts': 'typescript', '.cts': 'typescript', '.tsx': 'tsx'}
CRG_LABELS = {'cpp': {'cpp', 'c'}, 'objc': {'objc', 'c'}, 'notebook': {'python', 'notebook'}}  # .h -> c, .ipynb -> python
HOW = {   # one line per language for the notes / run.json
    'javascript': 'treesitter-module.ipynb rules', 'typescript': 'treesitter-module.ipynb rules',
    'tsx': 'treesitter-module.ipynb rules', 'python': 'treesitter-module.ipynb rules',
    'java': 'treesitter-module.ipynb rules',
    'hcl': 'tree-sitter hcl, top-level blocks', 'sql': 'line rule (PL/SQL)', 'robot': 'line rule (no grammar)',
    'xslt': 'stdlib expat (no grammar)', 'rescript': 'line rule (no grammar)', 'vbnet': 'line rule (no grammar)',
    'svelte': '<script> blocks + JS rules', 'vue': '<script> blocks + JS rules', 'notebook': 'joined code cells + Python rules',
}
NO_FUNCTIONS = {'html', 'json', 'yaml', 'properties', 'liquid'}             # survey languages with nothing to score
PROPOSAL_ONLY = {'cobol', 'vb6'}                                           # see notes/07: no grammar, proposal only


def grammar_for(lang, rel):
    if lang in LEGACY:
        return JS_GRAMMAR.get(Path(rel).suffix.lower()) if lang in ('javascript', 'typescript', 'tsx') else lang
    return RULES[lang]['grammar'] if lang in RULES else lang


def module_functions(root, rel, lang):
    """Every function the module finds in one file of a LANG repo."""
    src = (root / rel).read_bytes()
    if lang in LINE_RULES:
        return LINE_RULES[lang](src, lang, rel)
    if lang in LEGACY:
        g = grammar_for(lang, rel)
        if g is None:                       # e.g. a .ts file in a JS repo: the tool labels it typescript, not ours
            g = 'typescript' if rel.endswith('.ts') else 'javascript'
        return legacy_functions(src, g, rel)
    if lang in ('cpp', 'objc') and rel.endswith('.c'):
        return generic_functions(src, 'c', rel)
    return generic_functions(src, lang, rel)
