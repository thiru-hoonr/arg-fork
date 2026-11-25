# JavaScript rules

Fork rules for crg's `javascript`, `typescript` and `tsx` files, on top of crg 2.4.0.
Each rule fixes one known mistake crg makes when it extracts a file, and changes nothing else.
The rules run once per file in `CodeParser.parse_bytes`, after crg's own extraction; a rule that
fails leaves the file as crg parsed it and logs a warning.

| # | Rule | Fixes | Added |
|---|---|---|---|
| 1 | [callback-parameters](01-callback-parameters.md) | `id => …` stored as a Function | 2026-11-25 |
