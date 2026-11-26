# comments-among-class-decorators

**What crg does wrong:** in `@Injectable()` / `// oxlint-disable-next-line …` / `export class AuthService {}` the comment sits inside the class declaration, before the code, so nothing follows it in its block.

**What this change does:** when the comment's parent declaration starts with a decorator and only decorators or comments come before the comment, the comment documents that declaration. A comment in the class body, inside a decorator's arguments, after the class, or between the class name and `{` documents nothing.

**Code:** `code_review_graph/parser.py` (section "JavaScript / TypeScript comments")
**Tests:** `tests/test_js_comment_nodes.py`

## Results

Measured 2026-11-26 with changes 1–10 on crg 2.4.0, compared with the reference parser. **expected** is what the reference finds; **right** is what crg finds that the reference also finds; **missed** is what crg does not find; **invented** is what crg finds that the reference does not. **before** is the graph without this change, **after** with it.

On crg 2.4.0 alone this moves no count on the 5 repos: their comments among a class's decorators sit above exported classes, which crg starts at the `class` line, below the comment, so change 5 already links them. With the fork rule `start_exported_decorated_classes_at_their_decorator`, the class starts at its first decorator, above the comment, and only this change links it: measured 2026-11-25 in a scratch copy with the fork rules, twenty missed 49 → 25 (all 24 such comments linked).

Not changed by this change:

- Comment nodes: unchanged on all 5 repos.
- File CONTAINS Comment edges: unchanged on all 5 repos.
- Comment DOCUMENTS code edges: unchanged on all 5 repos.

## History

- 2026-11-26: added (SYN-F-616).
