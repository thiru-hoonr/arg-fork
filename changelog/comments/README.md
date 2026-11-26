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
