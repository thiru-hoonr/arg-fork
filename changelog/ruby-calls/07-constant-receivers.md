# constant-receivers

**What crg does wrong:** crg 2.4.0 reads which class a Ruby call is made on
(`_collect_ruby_test_receivers`: a constant `Devise.paranoid`, a constructor `Shop::Cart.new`, a
local last assigned from one) and stamps the call with it (`receiver_binding: class`,
`receiver_class`, `receiver_constant`, `receiver_scopes`), but only in a test file
(`parser.py:4338`). In library code the call keeps no receiver, so crg's resolver never follows
`Devise.paranoid` from `models/lockable.rb` to `def self.paranoid` (or `mattr_accessor :paranoid`)
in `lib/devise.rb`. On devise, 117 real calls were missed this way (SYN-F-633).

**What this change does:** the twin is the same source in a test file. The rule reads the
receivers with crg's own reader and gives each call whose receiver the file names as a class the
stamp crg gives it there. crg's resolver then links it to the method of that class in any file,
looking in the modules written around the call first, and to no other method of that name. A
receiver the file says nothing about (`item.total`), which crg stamps `unknown` in a test, is left
alone: in library code that stamp would take away links that are right today (on devise, 15 links
on objects are right because their class includes the module that defines the method). A call crg
already stamped, in a test file, is left as it is.

The rule runs first, as crg stamps a test file's receivers while it extracts: every later rule
reads a library call on a class the way it reads the same call in a test (`Cart.limit = 3`, which
change 1 renames, keeps its stamp as `limit=`).

**Code:** `code_review_graph/rules/ruby/constant_receivers.py` (rule `constant-receivers`); it
relies on `CodeParser._collect_ruby_test_receivers`, listed in `rules/__init__.py`.
**Tests:** `tests/test_ruby_constant_receiver_names_its_class_in_library_code.py` (32 tests; 13
fail with the rule off; 12 mutations of the rule, each caught). It reads every call's receiver in
order, so in a test file a call crg already stamped keeps its own entry (crg's own
`test_ruby_receiver_evidence_stays_with_its_own_call` caught the first version moving a stamp
onto the next call of the same name).

## Results

Measured 2026-11-27 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1–6, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 386 → 504 | 637 → 519 | 42 → 41 |

No right link is lost. The change stamps 276 CALLS edges on devise with
`fork_rule = constant-receivers`.

- +118 right: 63 calls on a constant or a constructed object (`Devise::Mapping.find_scope!`,
  `Devise.mappings`), 44 calls to methods a macro made in `lib/devise.rb` (`Devise.paranoid`,
  `Devise.responder`), 10 `X.new` calls crg now follows to `initialize` in another file, 1 call
  crg had linked to another method of the same name.
- −2 invented, +1 invented: the new one is `config.strategy_name` (`lib/devise.rb:463`, `config`
  assigned from `Devise::OmniAuth::Config.new`) linked to its `attr_reader`; the link is right,
  but `attr_*` methods are C methods, which TracePoint's `:call` never records.

**Whole-file twin check** (every library Ruby file, parsed as itself and as a test file; the calls
stamped with a class must be the same): devise 138 files, sinatra 72, jekyll 162, mastodon 2,238;
no file differs.

**Timing:** parsing every devise Ruby file takes 0.93 s with the rule off and 1.05 s with it on
(197 files, best of 5). The rule parses each file's tree once more.

## Known limits

- A receiver the file does not name as a class (an argument, `@ivar`, the result of another call)
  stays unstamped; its class needs types.

## History

- 2026-11-27: added (SYN-F-633).
