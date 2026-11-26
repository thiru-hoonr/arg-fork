# JavaScript and TypeScript comments

Every comment in a `javascript`, `typescript` or `tsx` file becomes a `Comment` node, on top of
crg 2.4.0: the file CONTAINS it, and a comment on its own line DOCUMENTS the function or class
below it. The comment pass runs at the end of `CodeParser._extract_bytes`; if it fails, the file
keeps crg's own nodes and edges and a warning is logged. Finding: SYN-F-616.

Results compare crg with the reference parser (`code-graph-rnd/js-ref`, TypeScript compiler) on
crm-prototype, nestjs-realworld-example-app, vue-element-admin, ghost and twenty. A count is
right / missed / invented; score = right ÷ (right + missed + invented).

| # | Change | Fixes | Added |
|---|---|---|---|
| 1 | [comment-nodes](01-comment-nodes.md) | comments dropped | 2026-11-26 |
| 2 | [full-text](02-full-text.md) | comment text missing | 2026-11-26 |
| 3 | [line-comment-runs](03-line-comment-runs.md) | one node per `//` line | 2026-11-26 |
| 4 | [file-contains-comments](04-file-contains-comments.md) | comments belong to nothing | 2026-11-26 |
| 5 | [comment-documents-code-below](05-comment-documents-code-below.md) | comments linked to no code | 2026-11-26 |
| 6 | [end-of-line-comments](06-end-of-line-comments.md) | end-of-line comments documenting the next line | 2026-11-26 |
| 7 | [comments-above-decorators](07-comments-above-decorators.md) | comments above decorators linked to nothing | 2026-11-26 |
| 8 | [comments-above-overloads](08-comments-above-overloads.md) | comments above TypeScript overloads linked to nothing | 2026-11-26 |
| 9 | [whole-declaration](09-whole-declaration.md) | comments linked to a callback inside a decorator | 2026-11-26 |
| 10 | [comments-among-class-decorators](10-comments-among-class-decorators.md) | comments between a class's decorators linked to nothing | 2026-11-26 |
