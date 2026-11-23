"""JavaScript fix rules that must run after every other per-file rule.

rules/ modules load in name order (all_languages, module_rules, ...), so this file is named zz_* to come last.

js.external-receiver
    ``nconf.get('url')`` where ``const nconf = require('nconf')``: the receiver is an outside npm package, so the
    call can never land on a function of this repository. CRG's post-build pass ``resolve_bare_call_targets`` binds
    a bare target to a same-named function when there is exactly one candidate in the call's file or an imported
    file, without looking at the receiver; once the fork stopped making duplicate nodes (js.functions-same-line)
    there was often exactly one ``get`` left (a test mock) and ``nconf.get`` was bound to it. This rule marks such
    calls unresolved (``unresolved_targets`` = [], which that pass skips) and records the package.
"""
from __future__ import annotations

import re

from . import rule

_REQUIRE = re.compile(r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""")
_IMPORT = re.compile(r"""\bimport\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"]""")


def _external(spec: str) -> bool:
    return not spec.startswith(('.', '/'))


def _external_receivers(source: bytes) -> dict[str, str]:
    text = source.decode('utf-8', errors='replace')
    out = {}
    for rx in (_REQUIRE, _IMPORT):
        for name, spec in rx.findall(text):
            if _external(spec):
                out.setdefault(name, spec)
    return out


@rule('javascript', 'js.external-receiver', kind='fix')
def external_receiver(ctx):
    """Calls on a receiver that is an outside npm package (require('pkg') / import x from 'pkg') stay unresolved."""
    pkgs = _external_receivers(ctx.source)
    if not pkgs:
        return
    for e in ctx.edges:
        if e.kind != 'CALLS' or '::' in str(e.target):
            continue
        ex = e.extra or {}
        spec = pkgs.get(ex.get('receiver'))
        if not spec:
            continue
        ex = {k: v for k, v in ex.items() if k != 'bare_call_target'}
        e.extra = ex
        ctx.tag(e, unresolved_targets=[], external_package=spec)
