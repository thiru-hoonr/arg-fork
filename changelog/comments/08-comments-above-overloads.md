# comments-above-overloads

**What crg does wrong:** crg makes a node only for an overloaded function's implementation, so a comment above `function echo(a: string): string;` documents nothing.

**What this change does:** same-name overload signatures (`function_signature`, `method_signature`, also inside `export`) are stepped over to reach the implementation. A signature followed by another name, an ambient `declare function`, or a signature last in a class documents nothing.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–8 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

**Comment DOCUMENTS code edges**

| repo | expected | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| crm-prototype | 56 | 56 → 56 | 0 → 0 | 1 → 1 |
| nestjs-realworld-example-app | 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| vue-element-admin | 56 | 55 → 55 | 1 → 1 | 5 → 5 |
| ghost | 8,809 | 8,146 → 8,153 | 663 → 656 | 1,167 → 1,167 |
| twenty | 2,748 | 2,676 → 2,678 | 72 → 70 | 331 → 331 |

Not changed by this change:

- Comment nodes: unchanged on all 5 repos.
- File CONTAINS Comment edges: unchanged on all 5 repos.

## History

- 2026-11-26: added (SYN-F-616).
