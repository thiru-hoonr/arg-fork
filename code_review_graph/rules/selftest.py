"""A rule that is off by default and exists only to prove the hook runs.

Turned on with CRG_RULES_ONLY=selftest.drop-calls, it drops every CALLS edge, so a build with it must differ
from stock in CALLS rows and nowhere else (notebooks/07-crg-fork/fork-noop-check.ipynb).
"""
from . import rule


@rule('*', 'selftest.drop-calls', kind='drop', default=False)
def drop_calls(ctx):
    """Test only: drop every CALLS edge."""
    ctx.edges = [e for e in ctx.edges if e.kind != 'CALLS']
