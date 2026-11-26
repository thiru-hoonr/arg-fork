# line-comment-runs

**What crg does wrong:** a three-line `//` comment is three Comment nodes, while the reference and a reader see one comment.

**What this change does:** own-line `//` comments on consecutive lines, with only whitespace and one line break between them, are one Comment node. A blank line splits them, and an end-of-line comment (`total = 1; // running total`) never merges with the comment below it.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–3 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

**Comment nodes**

| repo | expected | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| crm-prototype | 270 | 242 → 270 | 28 → 0 | 386 → 0 |
| nestjs-realworld-example-app | 5 | 5 → 5 | 0 → 0 | 0 → 0 |
| vue-element-admin | 258 | 231 → 258 | 27 → 0 | 65 → 0 |
| ghost | 37,540 | 29,457 → 37,540 | 8,083 → 0 | 23,354 → 0 |
| twenty | 11,521 | 8,649 → 11,521 | 2,872 → 0 | 8,139 → 0 |

Not changed by this change:

- File CONTAINS Comment edges: none yet; change 4 adds them.
- Comment DOCUMENTS code edges: none yet; change 5 adds them.

## History

- 2026-11-26: added (SYN-F-616).
