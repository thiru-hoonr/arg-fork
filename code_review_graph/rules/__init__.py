"""Post-parse rules: our additions on top of code-review-graph's own parser.

``CodeParser.parse_bytes`` calls :func:`apply` once per file, right after CRG's own extraction and
before test marking. A rule gets the file's nodes and edges and may add, fix or drop them. CRG's
language code (parser.py) is never edited; every rule lives in ``rules/<language>.py``.

Writing a rule::

    from code_review_graph.rules import rule

    @rule('go', 'go.calls.drop-type-conversions', kind='drop')
    def drop_type_conversions(ctx):
        ctx.edges = [e for e in ctx.edges if not (e.kind == 'CALLS' and e.target in GO_TYPES)]

- ``language`` is CRG's language name (``detect_language``), or ``'*'`` for every language.
- ``name`` is unique; everything a rule adds or changes should carry ``extra['rule'] = name``
  (``ctx.tag(obj)`` does it) so the measuring notebooks can count each rule's rows.
- ``kind`` is ``'add'``, ``'fix'`` or ``'drop'`` (documentation and reporting only).
- Rules run in registration order; a rule that raises is logged and its changes are discarded,
  so a broken rule never breaks a build.

Switching rules on and off (checked on every call, so a notebook can change them between builds):

- ``CRG_RULES=off``                 no rules at all (CRG exactly as upstream)
- ``CRG_RULES_ONLY=name1,name2``    run only these
- ``CRG_RULES_OFF=name1,name2``     run all except these
- a rule registered with ``default=False`` runs only when named in ``CRG_RULES_ONLY``
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class Rule:
    language: str
    name: str
    kind: str
    func: Callable
    default: bool = True
    doc: str = ''


@dataclass
class Ctx:
    """What a rule sees for one file. Rules replace or edit ``nodes`` / ``edges`` in place."""
    language: str
    path: Path
    source: bytes
    nodes: list
    edges: list
    rule: str = ''
    repo_root: str | None = None            # the repository being built (CodeParser._repo_root), when known
    _cache: dict = field(default_factory=dict)

    @property
    def crg_language(self):
        return self.language

    def tag(self, obj, **extra):
        """Mark a NodeInfo/EdgeInfo as written or changed by the current rule; returns it."""
        obj.extra = {**(obj.extra or {}), 'rule': self.rule, **extra}
        return obj

    def cache(self, key, make):
        """Per-file memo shared by the rules of one file (e.g. one tree-sitter parse)."""
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]


REGISTRY: list[Rule] = []
_loaded = False
stats: dict[str, dict[str, int]] = {}      # rule name -> counters, for the measuring notebooks


def rule(language: str, name: str, kind: str = 'fix', default: bool = True):
    if kind not in ('add', 'fix', 'drop'):
        raise ValueError(f'rule {name}: kind must be add, fix or drop')

    def wrap(func):
        if any(r.name == name for r in REGISTRY):
            raise ValueError(f'duplicate rule name {name}')
        REGISTRY.append(Rule(language, name, kind, func, default, (func.__doc__ or '').strip()))
        return func
    return wrap


def load():
    """Import every rules/<module>.py once so their @rule decorators register."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for m in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if not m.name.startswith('_'):
            importlib.import_module(f'{__name__}.{m.name}')


def _names(var):
    return {s.strip() for s in os.environ.get(var, '').split(',') if s.strip()}


def switch_on(name: str) -> bool:
    """On/off for a fork rule that is not a per-file @rule (post-build passes, graph.py tweaks).
    Same switches: CRG_RULES=off, CRG_RULES_ONLY, CRG_RULES_OFF; on by default."""
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return False
    only, off = _names('CRG_RULES_ONLY'), _names('CRG_RULES_OFF')
    return name in only if only else name not in off


def active(language: str) -> list[Rule]:
    if os.environ.get('CRG_RULES', '').lower() == 'off':
        return []
    load()
    only, off = _names('CRG_RULES_ONLY'), _names('CRG_RULES_OFF')
    out = []
    for r in REGISTRY:
        if r.language not in ('*', language):
            continue
        if only:
            if r.name in only:
                out.append(r)
        elif r.default and r.name not in off:
            out.append(r)
    return out


def apply(language, path, source, nodes, edges, repo_root=None):
    """Run the active rules for this file's language. Returns (nodes, edges)."""
    if not language:
        return nodes, edges
    rules = active(language)
    if not rules:
        return nodes, edges
    cache = {}                                              # shared by the rules of one file
    for r in rules:
        ctx = Ctx(language, Path(path), source, list(nodes), list(edges), r.name, repo_root, cache)
        s = stats.setdefault(r.name, {'files': 0, 'errors': 0, 'nodes +': 0, 'nodes -': 0, 'edges +': 0, 'edges -': 0})
        try:
            r.func(ctx)
        except Exception:                                   # a broken rule must never break a build
            logger.exception('rule %s failed on %s; its changes are discarded', r.name, path)
            s['errors'] += 1
            continue
        s['files'] += 1
        before_n, before_e = {id(x) for x in nodes}, {id(x) for x in edges}
        after_n, after_e = {id(x) for x in ctx.nodes}, {id(x) for x in ctx.edges}
        s['nodes +'] += len(after_n - before_n); s['nodes -'] += len(before_n - after_n)
        s['edges +'] += len(after_e - before_e); s['edges -'] += len(before_e - after_e)
        nodes, edges = ctx.nodes, ctx.edges
    return nodes, edges


def describe() -> list[dict]:
    """Every registered rule, for reports."""
    load()
    return [{'language': r.language, 'name': r.name, 'kind': r.kind, 'default': r.default, 'doc': r.doc} for r in REGISTRY]
