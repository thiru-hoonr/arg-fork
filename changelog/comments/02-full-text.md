# full-text

**What crg does wrong:** a Comment node alone does not say what the comment says.

**What this change does:** the comment's whole source text, from its first character to its last, is stored in `extra["text"]`, `@param` and `@returns` included, with no length cap (a 5 MB comment is kept whole). crg's own 400-character `docstring` field is left as it is.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–2 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

**Comment text** (`text_check.py`: each Comment node crg finds right is compared with the reference's exact source text of that comment)

| repo | comments crg finds right | with exactly the reference's text (before → after) | with no text (before → after) |
|---|---:|---:|---:|
| crm-prototype | 242 | 0 → 242 | 242 → 0 |
| nestjs-realworld-example-app | 5 | 0 → 5 | 5 → 0 |
| vue-element-admin | 231 | 0 → 231 | 231 → 0 |
| ghost | 29,431 | 0 → 29,431 | 29,431 → 0 |
| twenty | 8,649 | 0 → 8,649 | 8,649 → 0 |

The text check counts a comment once per file, first line and last line, so two comments on one line count once (ghost 29,431 here, 29,457 Comment nodes right in change 1's table).

Not changed by this change:

- Comment nodes: unchanged on all 5 repos.
- File CONTAINS Comment edges: none yet; change 4 adds them.
- Comment DOCUMENTS code edges: none yet; change 5 adds them.

## History

- 2026-11-26: added (SYN-F-616).
