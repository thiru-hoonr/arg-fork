# ancestor-order (class-parents order, Ruby walk in inheritance.py)

**What crg does wrong:** crg's inheritance walk (`InheritanceIndex._walk`) treats a class with more
than one parent the way it treats multiple inheritance: it asks every ancestor, and gives up when
any parent is outside the graph or two ancestors declare the method (`_only_declaration`). Ruby has
one lookup order, `Module#ancestors`: the modules the class includes, the last included first,
each followed by the modules it includes, then the superclass and its ancestors. The first
ancestor that declares the method wins (SYN-F-635):

```ruby
class SimpleFormForGenerator < Rails::Generators::Base   # outside the graph
  include ViewPathTemplates                               # declares copy_views
  def copy_views
    super        # Ruby runs ViewPathTemplates#copy_views; crg 2.4.0 + changes 1–9: no link
  end
end
```

**What this change does** (two parts):

1. Rule `class-parents` (change 8) writes its parents in Ruby's lookup order, and each INHERITS
   edge carries `extra["ruby_ancestor"]`, the parent's place in that order from 1: the last
   `include` statement first (`include A, B` puts `A` before `B`), then the superclass.
2. `InheritanceIndex._walk` (`inheritance.py`): for a Ruby class, `_ruby_walk` searches the
   ancestors depth first in that order (a module's own includes before the next parent), each
   once, and returns the first that declares the method. A parent outside the graph met before
   any declaration ends the walk with no answer, since it may declare the method. Marked
   `crg-fork (SYN-F-635)`. Other languages are unchanged.

No twin: crg writes no Ruby parents; the expected order is Ruby's, written by hand.

**Other tests:** change 8's tag test now expects the `ruby_ancestor` key.

**Tests:** `tests/test_ruby_method_lookup_follows_ancestor_order.py`: 14 tests, 10 fail with the
change off. 10 mutations of the two parts, each caught
(`lab/ruby-calls-changelog/ancestor-order-scratch/mutate.log`; the walk without its "each once"
check loops for ever on two modules that include each other, which the test
`test_modules_that_include_each_other_end_the_walk` catches by timing out).

## Results

Measured 2026-11-27 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1–9, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 650 → 651 | 373 → 372 | 50 → 50 |

No right link is lost and nothing is invented; every other row is unchanged.

- +1 right: `views_generator.rb:90` `super` → `ViewPathTemplates#copy_views`.
- The change matters little on devise because its classes rarely include a module and have a
  parent outside the graph at once; it also makes crg pick Ruby's answer where two ancestors
  declare the method (an included module over the superclass), where crg gave none.

**Timing:** parsing every devise Ruby file takes 1.20 s with rule `class-parents` off and 1.24 s
with it on (197 files, best of 5); the walk runs after the build.

## Known limits

- `prepend` and `extend` are not parents (change 8), so neither is in the order.
- Ruby skips a module already among the superclass's ancestors when it is included again; the walk
  searches it at its first place in depth-first order instead.

## History

- 2026-11-27: added (SYN-F-635).
