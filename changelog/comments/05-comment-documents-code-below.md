# comment-documents-code-below

**What crg does wrong:** nothing links `// Loads a lead.` to the `load` function under it.

**What this change does:** a comment DOCUMENTS the next code in its block, however many blank lines sit between (senior's decision), when crg has a node starting there. Nothing is documented when the code below is not a node (`const retries = 3;`), is an `import` (a header comment), or when the comment is last in its block.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–5 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

**Comment DOCUMENTS code edges**

| repo | expected | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| crm-prototype | 56 | 0 → 56 | 56 → 0 | 0 → 1 |
| nestjs-realworld-example-app | 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| vue-element-admin | 56 | 0 → 55 | 56 → 1 | 0 → 5 |
| ghost | 8,809 | 0 → 8,146 | 8,809 → 663 | 0 → 1,184 |
| twenty | 2,748 | 0 → 2,482 | 2,748 → 266 | 0 → 333 |

Not changed by this change:

- Comment nodes: unchanged on all 5 repos.
- File CONTAINS Comment edges: unchanged on all 5 repos.

## History

- 2026-11-26: added (SYN-F-616).
