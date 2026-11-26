# file-contains-comments

**What crg does wrong:** a Comment node has no edge, so it is not reached from its file.

**What this change does:** each Comment gets a File CONTAINS edge, wherever it sits. A comment inside a function or class records that code node's qualified name in `extra["enclosing"]`: the nearest tree ancestor whose lines match a crg node. A top-level comment has no `enclosing`.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–4 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

**File CONTAINS Comment edges**

| repo | expected | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| crm-prototype | 270 | 0 → 270 | 270 → 0 | 0 → 0 |
| nestjs-realworld-example-app | 5 | 0 → 5 | 5 → 0 | 0 → 0 |
| vue-element-admin | 258 | 0 → 258 | 258 → 0 | 0 → 0 |
| ghost | 37,540 | 0 → 37,540 | 37,540 → 0 | 0 → 0 |
| twenty | 11,521 | 0 → 11,521 | 11,521 → 0 | 0 → 0 |

Not changed by this change:

- Comment nodes: unchanged on all 5 repos.
- Comment DOCUMENTS code edges: none yet; change 5 adds them.

## History

- 2026-11-26: added (SYN-F-616).
