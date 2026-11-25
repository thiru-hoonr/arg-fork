# callback-parameters

**What crg does wrong:** `ids.map(id => load(id))` gives a Function `id` that owns the call to
`load`. The same callback written `(id) => load(id)` gives no Function, and the call belongs to the
enclosing function.

**What the rule does:** removes the fake Function and gives its calls to the enclosing function or
file, as crg does for the twin. A call that pointed at the removed Function points at the plain name
again, and a test owner gets its TESTED_BY edge. Every edge the rule changes carries
`extra["fork_rule"] = "callback-parameters"`.

**Code:** `code_review_graph/rules/javascript/callback_parameters.py`
**Tests:** `tests/test_callback_parameter_is_not_a_function.py`

## Results

Measured 2026-11-25, this rule alone vs crg 2.4.0. JS/TS Functions, compared with the reference
parser (`code-graph-rnd/js-ref`).

| repo | right (before / after) | callback Functions | Functions removed |
|---|---|---|---|
| crm-prototype | 206 / 206 | 35 → 0 | 35 |
| nestjs-realworld-example-app | 49 / 49 | 16 → 0 | 16 |
| vue-element-admin | 179 / 179 | 78 → 0 | 78 |
| ghost | 24,607 / 24,607 | 236 → 0 | 236 |
| twenty | 68,779 / 68,779 | 4 → 0 | 4,032 |

- No correct Function is lost and no missed count changes in any repo.
- Every removed Function is a parameter written `name =>` on its line; no node is added and no other
  node kind changes.
- In twenty, 4,028 of the removed Functions are inside committed bundles
  (`.yarn/releases/*.cjs`, generated seed-project `index.mjs`).
- Full lists: team workbook, `Function-<repo>` sheets.

## Known limits

- Owner lookup in one-line minified bundles (SYN-F-582).

## History

- 2026-11-25: added.
