"""Module (tree-sitter) classes and edges for Verilog/SystemVerilog. Conventions as edgeref_verilog.py (slang):
class = module/interface/program/package/class declaration; inherits = class extends / implements (last :: part);
import = `include directive + package_import_declaration; call = tf_call / method_call / system_tf_call (last name),
plus a statement-level `f(args);` that tree-sitter-verilog misparses as checker_instantiation."""
import re
from pathlib import Path
from tree_sitter_language_pack import get_parser
LABEL = ("tree-sitter verilog: module/interface/program/package/class declarations, extends/implements, "
         "`include + package import, tf_call/method_call/system_tf_call (+ statement checker_instantiation)")
DECL = {'module_declaration': 'module', 'interface_declaration': 'interface', 'program_declaration': 'program',
        'package_declaration': 'package', 'class_declaration': 'class', 'interface_class_declaration': 'class'}
IDS = ('module_identifier', 'interface_identifier', 'program_identifier', 'package_identifier', 'class_identifier')

def _walk(n):
    st = [n]
    while st:
        x = st.pop(); yield x; st.extend(reversed(x.children))

NAMEY = IDS + ('simple_identifier', 'escaped_identifier')

def _decl_name(n):
    # the name is a direct child of the declaration or of its *_header (never an identifier inside ports/imports)
    for c in n.named_children:
        if c.type in NAMEY: return c.text.decode()
        if c.type.endswith('_header'):
            for h in [c] + [x for x in c.named_children if x.type.endswith('_header')]:
                for x in h.named_children:
                    if x.type in NAMEY: return x.text.decode()
    return None

HEAD = re.compile(rb'^[ \t]*(?:\(\*.*?\*\)\s*)?(?:virtual\s+|interface\s+(?=class)|extern\s+)?(module|macromodule|interface|program|package|class)\s+(?:(?:automatic|static)\s+)?([A-Za-z_]\w*)', re.M)

def _fallback(src, have):
    """tree-sitter-verilog drops a declaration whose header it cannot parse (ERROR): read the header keyword line."""
    out = []
    code = re.sub(rb'/\*.*?\*/', lambda m: re.sub(rb'[^\n]', b' ', m.group()), src, flags=re.S)
    code = re.sub(rb'//[^\n]*', b'', code)
    for m in HEAD.finditer(code):
        kw, nm = m.group(1).decode(), m.group(2).decode()
        L = code.count(b'\n', 0, m.start(1)) + 1
        if (L, nm) not in have and nm not in ('class',):
            out.append((L, nm, {'macromodule': 'module'}.get(kw, kw)))
    return out

def _last_simple(n):
    ids = [x for x in _walk(n) if x.type in ('simple_identifier', 'escaped_identifier', 'system_tf_identifier')]
    return ids[-1] if ids else None

PP = re.compile(rb'^[ \t]*`(ifdef|ifndef|elsif|else|endif|define|undef)\b[ \t]*(\w*)')

def _inactive(src):
    """Lines in a `ifdef/`ifndef branch that is off with no command-line defines (slang only parses active text)."""
    defined, stack, off = set(), [], set()
    for i, ln in enumerate(src.split(b'\n'), 1):
        m = PP.match(ln)
        if m:
            d, nm = m.group(1).decode(), m.group(2).decode()
            if d == 'define' and all(a for a, _ in stack): defined.add(nm)
            elif d == 'undef' and all(a for a, _ in stack): defined.discard(nm)
            elif d in ('ifdef', 'ifndef'):
                on = (nm in defined) == (d == 'ifdef'); stack.append((on, on))
            elif d == 'elsif' and stack:
                _, taken = stack[-1]; on = not taken and nm in defined; stack[-1] = (on, taken or on)
            elif d == 'else' and stack:
                _, taken = stack[-1]; stack[-1] = (not taken, True)
            elif d == 'endif' and stack: stack.pop()
            continue
        if not all(a for a, _ in stack): off.add(i)
    return off

def edges(root, files):
    p = get_parser('verilog'); out = []
    for rel in files:
        src = (Path(root) / rel).read_bytes(); t = p.parse(src); start = len(out)
        for n in _walk(t.root_node):
            L = n.start_point[0] + 1
            if n.type in DECL:
                nm = _decl_name(n)
                if nm is None: continue
                out.append(dict(e='class', file=rel, line=L, name=nm, kind=DECL[n.type]))
                if DECL[n.type] == 'class':
                    for c in n.named_children:
                        if c.type in ('class_type', 'interface_class_type'):
                            out.append(dict(e='inherits', file=rel, line=L, cls=nm, sup=c.text.decode().split('#')[0].split('::')[-1].strip()))
            elif n.type == 'include_compiler_directive':
                s = next((c for c in n.named_children if c.type in ('double_quoted_string', 'string_literal')), None)
                out.append(dict(e='import', file=rel, line=L, name=s.text.decode().strip('"<>') if s else n.text.decode()))
            elif n.type == 'package_import_declaration':
                for it in [c for c in n.named_children if c.type == 'package_import_item'] or [n]:
                    out.append(dict(e='import', file=rel, line=L, name=it.text.decode()))
            elif n.type == 'system_tf_call':
                i = next((c for c in n.named_children if c.type == 'system_tf_identifier'), None)
                if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=L, name=i.text.decode()))
            elif n.type == 'tf_call':
                ids = [c for c in n.named_children if c.type in ('simple_identifier', 'escaped_identifier')]
                if ids: out.append(dict(e='call', file=rel, line=ids[-1].start_point[0] + 1, start=L, name=ids[-1].text.decode()))
            elif n.type == 'method_call':
                b = next((c for c in n.named_children if c.type == 'method_call_body'), None)
                i = _last_simple(b) if b is not None else None
                if i is not None and b'(' in b.text: out.append(  # `a.b.last` (no parens) is member access
                    dict(e='call', file=rel, line=i.start_point[0] + 1, start=L, name=i.text.decode()))
            elif n.type == 'system_tf_identifier' and n.parent.type != 'system_tf_call':
                out.append(dict(e='call', file=rel, line=L, start=L, name=n.text.decode()))   # $f( inside an ERROR
            elif n.type == 'simple_identifier' and n.prev_sibling is not None and n.prev_sibling.type == 'package_scope' \
                    and n.next_sibling is not None and n.next_sibling.type == 'ERROR' and n.parent.type not in ('tf_call', 'method_call_body'):
                out.append(dict(e='call', file=rel, line=L, start=L, name=n.text.decode()))   # pkg::f(...) left as ERROR
            elif n.type == 'checker_instantiation' and n.parent is not None and n.parent.type == 'statement_item':
                i = next((c for c in n.named_children if c.type == 'checker_identifier'), None)
                if i is not None: out.append(dict(e='call', file=rel, line=i.start_point[0] + 1, start=L, name=i.text.decode()))
        # `new(..)` is a constructor expression and `void'(..)` a cast in slang, not InvocationExpression
        off = _inactive(src)
        out[start:] = [r for r in out[start:] if not (r['e'] == 'call' and r['name'] in ('new', 'void')) and r['line'] not in off]
        have = {(r['line'], r['name']) for r in out[start:] if r['e'] == 'class'}
        # keep only the fallback headers whose name tree-sitter did not give at all; drop tree rows at a wrong line
        names = {}
        for L, nm, k in _fallback(src, have): names.setdefault(nm, (L, k))
        good = {(L, nm) for nm, (L, k) in names.items()}
        out[start:] = [r for r in out[start:] if r['e'] != 'class' or r['name'] not in names or (r['line'], r['name']) in good]
        have = {r['name'] for r in out[start:] if r['e'] == 'class'}
        out += [dict(e='class', file=rel, line=L, name=nm, kind=k) for nm, (L, k) in names.items() if nm not in have]
    return out
