# whole-declaration

**What crg does wrong:** on `@Query(() => Boolean)` above `isStopped()`, crg makes a Function `Boolean` (SYN-F-581) that starts on the decorator line, and the comment documents it instead of the method; a comment above a decorated field (`@Field(() => String)` / `name: string;`) documents the fake `String`.

**What this change does:** a comment documents only a node that ends where the declaration below it ends.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–9 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

**Comment DOCUMENTS code edges**

| repo | expected | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| crm-prototype | 56 | 56 → 56 | 0 → 0 | 1 → 1 |
| nestjs-realworld-example-app | 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| vue-element-admin | 56 | 55 → 55 | 1 → 1 | 5 → 5 |
| ghost | 8,809 | 8,153 → 8,151 | 656 → 658 | 1,167 → 1,123 |
| twenty | 2,748 | 2,678 → 2,688 | 70 → 60 | 331 → 252 |

Not changed by this change:

- Comment nodes: unchanged on all 5 repos.
- File CONTAINS Comment edges: unchanged on all 5 repos.

## Known limits

- On this branch alone, `it.each(table)('…', fn)` tests lose their link, because crg 2.4.0 stores them as only the inner `it.each(table)` call (SYN-F-584); the fork rule `store_each_tables_as_the_outer_call` brings them back.
- License headers of committed bundles lose their link to a minified function (SYN-F-582).

## History

- 2026-11-26: added (SYN-F-616).
