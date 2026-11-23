#!/usr/bin/env bash
# Run the forked code-review-graph without installing it: .venv-crg240's python (all deps there) + crg-fork first on the path.
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" exec "$HERE/../.venv-crg240/bin/python" -m code_review_graph "$@"
