# class-hierarchy (class-parents, self-receivers, Ruby constants in inheritance.py)

**What crg does wrong:** crg 2.4.0 has an inheritance index (`inheritance.py`) and a resolver that
looks a call on `self` up in the caller's class and then its parents (`graph.py`
`_class_backed_candidates`), but gives them nothing for Ruby (SYN-F-633):

- `_get_bases` (`parser.py:23806`) reads base classes for 13 languages and none for Ruby, so a
  Ruby class has no INHERITS edge (0 on devise).
- The resolver walks the parents only for a call whose `extra` says `receiver: self`; crg writes no
  receiver for a Ruby call.
- `InheritanceIndex.resolve_type` finds a parent only in the same file or a `require`d one, and
  does not split a `::` path. Rails loads `DeviseController` without a `require`.

So `self.resource = x` in `Devise::ConfirmationsController` never reaches
`DeviseController#resource=`, and neither do `resource_class`, `resource_params` and every other
call a controller makes to its parent.

**What this change does** (three parts, which only work together; each alone links nothing):

1. Rule `class-parents` (`rules/ruby/class_parents.py`): an INHERITS edge from each class or module
   to its superclass written as a constant (`< DeviseController`, `< Shop::Base`, `< ::Base`) and
   to each module a statement of its body includes by a constant (`include A, B::C`), in the form
   crg writes a base for other languages: target the name as written, line the class's first
   line. `extend`, parents built at run time, and `include` on a receiver, in a method or in a
   block are left alone.
2. Rule `self-receivers` (`rules/ruby/self_receivers.py`): `receiver: self` on each call in a
   method of a class that has no receiver or the receiver `self`: a call crg's receiver reader
   lists without one, a bare name (rule `bare-name-calls`), a setter call `self.x = v` (rule
   `setter-assignments`). The target becomes the plain name, so the resolver looks it up: the
   class's own method first, then its parents'. A call crg linked in the file to a method of the
   caller's own class, or to a top-level `def`, is left as it is; one it linked to a same-named method of another class in
   the file is not (`target_path` in `MarkerbGenerator` was linked to `ViewPathTemplates`).
   A call in a class method (`def self.x`, a `def` in `class << self`) also carries
   `ruby_class_method`: there `self` is the class, so crg's resolver (`graph.py`, marked
   `crg-fork (SYN-F-641)`) takes only the class's own method (`mattr_accessor :router_name` makes
   one) and never walks to an inherited or included instance method.
3. `InheritanceIndex._resolve_parent` (`inheritance.py`): a Ruby parent is found by Ruby's constant
   lookup: in the modules written around the class, innermost first, then at the top level;
   `::Base` is the top level only; a Ruby class only; two matches give none. This is the one
   change to crg itself, marked `crg-fork (SYN-F-633)`.

No twin: crg writes neither a Ruby INHERITS edge nor a Ruby receiver, so the expected edges and
marks are written by hand, in the form crg uses for other languages.

**Shared code:** `crg_class()` (crg's Class node for a `class` / `module` in the tree) moved from
change 4's `macro_methods.py` to `rules/ruby/_syntax.py`; change 4 now uses it.

**Other tests:** rule `self-receivers` marks calls changes 1 and 4 made, and its tag replaces theirs
(as each later rule's does); their two tag tests now run with `self-receivers` off.

**Tests:**
- `tests/test_ruby_superclass_and_include_are_parents.py` (rule `class-parents`);
- `tests/test_ruby_call_without_receiver_is_on_self.py` (rule `self-receivers`);
- `tests/test_ruby_call_reaches_the_method_its_class_inherits.py` (the three together, after a
  full build and crg's resolution, checking the stored link).

51 tests; 28 fail with the two rules and the `inheritance.py` change off (6 of the 8 first
full-build tests failed with only the `inheritance.py` change off). 29 mutations of the three
parts, each caught. crg's own `test_ruby_bare_call_prefers_a_top_level_method_when_no_class_defines_it`
caught a first version that also re-resolved a call crg linked to a top-level `def`; such a call
is now left as it is.

## Results

Measured 2026-11-27 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1–7, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 504 → 649 | 519 → 374 | 41 → 50 |

No right link is lost. The change adds 229 INHERITS edges on devise and marks 4,210 CALLS edges
`receiver: self`.

- +145 right: 70 calls to a method inherited from a parent class in another file (the Devise
  controllers' calls into `DeviseController`), 49 calls crg had left unlinked because the file
  defines the name twice, 15 setter calls `self.resource = …` (to the `def resource=` change 6
  added), 9 calls to a method of an included module, 1 method a macro made, 1 call crg had linked
  to another method of that name.
- +10 invented, all calls that sit behind a condition that never held in devise's tests, so the
  answer key cannot judge them: `yield resource if block_given?` (9 controllers' lines; the tests
  pass no block) and a ternary branch (`registrations_controller.rb:67`). Each links
  `resource` / `resource_name` to `DeviseController`, which is right. −1 invented.
- Split, measured with prototypes: the superclass edges give +134, the `include` edges +9; the
  `inheritance.py` change and the `receiver: self` marks are each required.

**Timing:** parsing every devise Ruby file takes 1.06 s with the two rules off and 1.22 s with
them on (197 files, best of 5).

## Known limits

- Ruby reads a constant in a class's body inside the class, and its superclass outside it; both are
  looked up from the modules around the class (`class A::B` is treated as written inside `A`).
- A class with a parent the graph cannot name (`< Devise.parent_controller.constantize`,
  `< ActionController::Base`) stops crg's walk at that parent, as crg does for other languages.
- Mixin calls still missed on devise (93): mostly modules included at run time (Rails `on_load`
  hooks, `devise :database_authenticatable`), which no class body names.

## History

- 2026-11-27: added (SYN-F-633).
- 2026-11-27: calls in class methods no longer reach inherited or included instance methods
  (SYN-F-641, found in code review). devise unchanged at 657/366/50. Whether a call is in a class
  method is decided by its line, since crg names `def self.build` and `def build` both `A.build`.
