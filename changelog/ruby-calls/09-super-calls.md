# super-calls (rule super-calls, Ruby `super` in graph.py)

**What crg does wrong:** Ruby's `super` runs the method of the same name one step up the class's
ancestors. tree-sitter-ruby gives it a `super` node, not a `call` with a method name, and crg 2.4.0
reads only `call` nodes, so it stores no call for any spelling of `super` (SYN-F-634):

```ruby
class Devise::Strategies::Authenticatable < Base
  def store?
    super && !mapping.to.skip_session_storage.include?(authentication_type)   # no call to Base#store?
  end
end
```

**What this change does** (two parts):

1. Rule `super-calls` (`rules/ruby/super_calls.py`), run last: for each `super` node in an instance
   method crg has a Function for, a CALLS edge from that method to its own name with
   `extra["receiver"] = "super"`, the receiver crg writes for `super.run()` in other languages. crg's
   resolver looks such a call up in the parents of the caller's class, never in the class itself
   (`GraphStore._class_backed_candidates`). `super`, `super(x)`, `super x`, `super do … end` and a
   `super` in a block of the method are each one call. A `super` in `def self.x`, in a top-level
   `def`, outside a method, or in a method crg cannot name for sure (two on one line) is left
   alone.
2. `GraphStore._class_backed_candidates` (`graph.py`): for a Ruby call on `super` whose parents
   hold no such method in the graph, no candidate, rather than crg's usual evidence. The usual
   evidence picks the caller's own same-named method, the one `super` never means; the method that
   runs is outside the graph (`ActiveRecord::Base#initialize`). Marked `crg-fork (SYN-F-634)`.
   Other languages are unchanged.

No twin: crg writes no call for any spelling of `super` in Ruby, so the expected edges are written
by hand, in the form crg uses for `super.run()` elsewhere.

**Tests:** `tests/test_ruby_super_calls_the_parent_method.py`: 17 tests, 12 fail with the rule and
the `graph.py` change off. 10 mutations of the two parts, each caught
(`lab/ruby-calls-changelog/super-calls-scratch/mutate.log`).

## Results

Measured 2026-11-27 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1–8, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 649 → 650 | 374 → 373 | 50 → 50 |

No right link is lost and nothing is invented. The rule adds 29 calls on `super`; 5 link, 24 stay
unlinked.

- +1 right: `strategies/authenticatable.rb:14` → `Base#store?`.
- 4 more links are into the devise controllers from the test app's controllers
  (`Custom::RegistrationsController#create` → `Devise::RegistrationsController#create`), which the
  answer key does not judge.
- Of the 12 `super` calls devise's tests make, 9 are in model modules (`Lockable`, `Confirmable`)
  whose parents are added at run time by `devise :lockable, …`; no source names them, and they stay
  missed. `views_generator.rb:90` misses because crg gives up on a class with a parent outside the
  graph (`< Rails::Generators::Base`) even though Ruby looks in its included module first: change 10.
  `controllers/helpers.rb:280` runs through an `alias`.

**Timing:** parsing every devise Ruby file takes 1.30 s with the rule off and 1.39 s with it on
(197 files, best of 5).

## Known limits

- `super` in a class method (`def self.x`) is left alone: crg's walk reads instance methods.
- `super` reaching a method through `alias` or `alias_method` is not followed.

## History

- 2026-11-27: added (SYN-F-634).
