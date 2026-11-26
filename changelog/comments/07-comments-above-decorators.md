# comments-above-decorators

**What crg does wrong:** in `// Lead store.` / `@Injectable()` / `export class Leads {}` the code after the comment is a decorator, so the comment documents nothing; the same for decorated methods.

**What this change does:** decorators between the comment and the code are skipped, and the crg node that starts between the first decorator and the declaration's own line is documented.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–7 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

**Comment DOCUMENTS code edges**

| repo | expected | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| crm-prototype | 56 | 56 → 56 | 0 → 0 | 1 → 1 |
| nestjs-realworld-example-app | 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| vue-element-admin | 56 | 55 → 55 | 1 → 1 | 5 → 5 |
| ghost | 8,809 | 8,146 → 8,146 | 663 → 663 | 1,167 → 1,167 |
| twenty | 2,748 | 2,482 → 2,676 | 266 → 72 | 323 → 331 |

Not changed by this change:

- Comment nodes: unchanged on all 5 repos.
- File CONTAINS Comment edges: unchanged on all 5 repos.

## History

- 2026-11-26: added (SYN-F-616).
