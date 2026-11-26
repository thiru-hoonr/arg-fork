# comment-nodes

**What crg does wrong:** crg 2.4.0 drops every comment in a JavaScript or TypeScript file, so nothing in the graph says what `/** Loads a lead. */` above a function is about.

**What this change does:** each comment, of all three styles (`//`, `/* */`, `/** */`), becomes a `Comment` node named `comment@<line>:<column>`. Comments are read from crg's own tree-sitter tree, so `//` inside JSX text or a string is not a comment. If the comment pass raises, the file keeps the nodes and edges crg made and a warning names the file.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–1 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

**Comment nodes**

| repo | expected | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| crm-prototype | 270 | 0 → 242 | 270 → 28 | 0 → 386 |
| nestjs-realworld-example-app | 5 | 0 → 5 | 5 → 0 | 0 → 0 |
| vue-element-admin | 258 | 0 → 231 | 258 → 27 | 0 → 65 |
| ghost | 37,540 | 0 → 29,457 | 37,540 → 8,083 | 0 → 23,354 |
| twenty | 11,521 | 0 → 8,649 | 11,521 → 2,872 | 0 → 8,139 |

Not changed by this change:

- File CONTAINS Comment edges: none yet; change 4 adds them.
- Comment DOCUMENTS code edges: none yet; change 5 adds them.

## Known limits

- An unclosed `/*` gives no comment: tree-sitter does not see one.

## History

- 2026-11-26: added (SYN-F-616).
