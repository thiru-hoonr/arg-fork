# bare-name-calls

**What crg does wrong:** crg 2.4.0 stores no call for a method called by its bare name, with no
receiver, no arguments and no parentheses: `if resource.save`, `helper(resource_name)`, `warden`.
tree-sitter-ruby parses the name as an `identifier`, and crg only reads `call` nodes. Ruby runs the
name as a method call unless it is a local variable at that point. On devise, crg missed 297 real
calls this way.

**What this change does:** an identifier is a call unless the name is a local variable in scope:
a parameter of the method, block or lambda (every kind, and block-local `|x; y|`), a name assigned
earlier in the same scope (Ruby decides as it reads, so `x = x` and a name assigned in an earlier
`if` branch are locals; multiple assignment, `rescue => e`, `for x in`, and pattern variables
count), or `it` / `_1`…`_9` in a block with no parameters. A block sees the locals around it; a
`def`, `class` or `module` starts empty, and a class's superclass and `class << obj` are read
outside it. Names in `defined?(…)`, `alias`, `undef`, method names and the object of
`def obj.name` are not calls, and neither are the keywords `__FILE__`, `__LINE__` and
`__ENCODING__`, which tree-sitter reads as identifiers (`__FILE__()` is not Ruby).

The twin is the same call written `name()`, which crg extracts. The rule writes the twin: it
inserts `()` after each bare call (byte offsets from the tree), runs crg's own extraction
(`CodeParser._extract_bytes`) on that source, and adds the twin's CALLS and TESTED_BY edges at
those places, tagged `fork_rule = bare-name-calls`. When crg had stored the name as a REFERENCES
edge (a bare name of a top-level `def` used as an argument, array element or hash value), the
rule drops it, as the twin has none. A file tree-sitter cannot parse cleanly (an ERB template) is
left as crg made it. All other nodes and edges are crg's. If crg's nodes have no File node, the
rule cannot know the file's path: it raises an error that says so, and the file keeps crg's parse
(the fork logs a warning).

**Code:** `code_review_graph/rules/ruby/bare_name_calls.py` (rule `bare-name-calls`)
**Tests:** `tests/test_ruby_bare_name_is_a_call_unless_a_local.py` (78 tests; 29 fail with the
rule off; 39 mutations of the rule, each caught)

**Shared helper:** `file_path_of(nodes)` in `code_review_graph/rules/ruby/_syntax.py` reads the
file's path from crg's File node and raises a clear error when there is none; later rules use it.

**Change to change 2's test:** in
`tests/test_ruby_class_receiver_call_is_not_an_instance_method.py`, line 2 of
`def new; resource_class.new; end` now also gets the call to `resource_class`, so the test checks
that the `new` call is unlinked instead of that `new` is the line's only call.

## Results

Measured 2026-11-26 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1 and 2, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) | not judged (before → after) |
|---|---:|---:|---:|---:|---:|
| devise | 1,023 | 209 → 354 | 814 → 669 | 26 → 29 | 10 → 23 |

Category "bare name, no arguments": 297 missed → 0. No right link is lost. The same build with
every one of the 297 sites written `name()` by hand gives the same 354 / 669: the rule reaches
the twin's ceiling. The other 152 of the 297 are now calls crg stores but cannot link (the method
is inherited 51, defined twice in the file 48, in a mixin 40, in the test app 3, or crg links
another method of that name 10). The 3 new invented links: 2 to another method with the same name
(`remember_token`, `target_path`, made by the twin build too), 1 a real call on a line whose
condition never fired (`raise_no_secret_key unless Devise.secret_key`). Not judged: 13 new links
on lines devise's tests never run.

The change tags 1,584 CALLS and 913 TESTED_BY edges on devise.

**Whole-file twin check** (every Ruby file, the rule's graph against crg's graph of the file with
`()` after every bare call): devise 0 of 188 files differ (9 ERB templates skipped), jekyll 0 of
162. Every edge the rule adds is in the twin's graph with the same `extra` (devise 2,546, jekyll
1,259, sinatra 3,625, mastodon 66,175), and the 4 it drops (jekyll REFERENCES) are not. What
still differs on sinatra (5 files) and mastodon (447, all specs) is crg's own edges, which crg
reads better without `()`: `subject.status` and `described_class.query` are bound to the
described class, `subject().status` is not (sinatra 30, mastodon 4,962 edges), and
`describe described_class::SubPolicy` names its test node differently (mastodon 6).

**Timing:** parsing every devise Ruby file takes 0.41 s with the rule off and 0.80 s with it on
(197 files, best of 5); sinatra 0.86 s → 1.49 s. The rule extracts each file with a bare call a
second time.

## Known limits

- Names made local by a named regex capture (`/(?<year>\d+)/ =~ s`) are read as calls.
- A bare Ruby keyword-like method (`private`, `raise`, `block_given?`) becomes a plain-name call,
  as `private()` does in crg.
- `subject` and `described_class` in a spec are calls, and get calls; crg's own reading of
  `subject.x` is kept.

## History

- 2026-11-26: added (SYN-F-629).
