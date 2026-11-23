"""Rules for every language."""
import re
from collections import Counter

from . import rule


def _short(target):
    """'file::Cls.m:S<hash>' / 'pkg.mod.f' -> 'f' (the name as written at the call site)."""
    t = re.sub(r':S[0-9a-f]{6,}(:\d+)?$', '', str(target))
    t = t.rsplit('::', 1)[-1]
    return re.split(r'[.\\/#]', t)[-1] or t


_CONT = ('.', '->', '?.', '::', ')', ']', '|>')


def _statement(lines, line, limit=40):
    """The text of the statement starting at `line`: parsers put every call of a chain split over lines
    (`request(app)` / `.get(..)` / `.expect(..)`) on its first line. Follows lines while brackets are open or the
    next line continues the chain; at most `limit` lines."""
    if not line or not 0 < line <= len(lines):
        return ''
    out, depth, i = [], 0, line - 1
    while i < len(lines) and len(out) < limit:
        t = lines[i]
        out.append(t)
        depth += t.count('(') + t.count('[') + t.count('{') - t.count(')') - t.count(']') - t.count('}')
        nxt = lines[i + 1].lstrip() if i + 1 < len(lines) else ''
        if depth <= 0 and not nxt.startswith(_CONT):
            break
        i += 1
    return '\n'.join(out)


@rule('*', 'calls.keep-same-line-repeats', kind='fix')
def keep_same_line_repeats(ctx):
    """Problem 15: f(f(x)) or two f() calls on one line are two CALLS edges from the parser, but the store keys an
    edge by (kind, source, target, file, line, extra.source_offset) and keeps only one. Give the 2nd, 3rd... repeat
    a source_offset of -1, -2... so every call is stored. Negative so it can never clash with the byte offsets CRG
    itself sets for C#; the first occurrence is left exactly as the parser made it.

    Safety check: a repeat is kept only while the statement's text (see _statement) really has that many
    occurrences of the called name. Note CRG files every call of a multi-line statement under the statement's
    first line (e.g. 12 `New QualityItem(..)` rows on one VB.NET line); those are real calls, kept here, and
    putting them on their own lines is a separate rule. Notebooks are not text-checked (cell line numbers)."""
    lines = ctx.cache('lines', lambda: ctx.source.decode('utf-8', 'replace').splitlines())
    check_text = ctx.language != 'notebook'      # .ipynb: lines are per cell, the raw file is JSON
    seen, room = Counter(), {}
    for e in ctx.edges:
        if e.kind != 'CALLS' or (e.extra or {}).get('source_offset') is not None:
            continue
        key = (e.source, e.target, e.file_path, e.line)
        n = seen[key]
        seen[key] += 1
        if not n:
            continue
        if key not in room:
            name = _short(e.target)
            text = _statement(lines, e.line)
            room[key] = len(re.findall(r'(?<![\w$])' + re.escape(name) + r'(?![\w$])', text)) if name else 0
        if not check_text or n < room[key]:
            ctx.tag(e, source_offset=-n)
