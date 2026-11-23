# module_src: the module's code, copied unchanged

Copied from the code-graph-rnd repo so the fork runs exactly the code that was measured:

- `extract.py`, `edges.py`, `classes.py` from `core-module/`
- `modref_<language>.py` from `lib/`

These files import each other by top-level name (`import extract`, `from modref_typescript import ...`), so
`rules/module_rules.py` puts this folder on `sys.path` before importing them. After changing a file in the R&D
repo, copy it again:

    cp core-module/{extract,edges,classes}.py lib/modref_*.py crg-fork/code_review_graph/rules/module_src/
