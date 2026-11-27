# runtime-dump (optional `ruby-runtime.json`, read in inheritance.py / graph.py)

**What crg does wrong:** many Ruby modules reach a class only at run time, where no static rule can
see them. devise includes its controller helpers through a method parameter inside a Rails hook,
into a class outside the graph, and its controllers' superclass is a string (SYN-F-637):

```ruby
def self.include_helpers(scope)
  ActiveSupport.on_load(:action_controller) { include scope::Helpers }   # rails.rb: include_helpers(Devise::Controllers)
end
class DeviseController < Devise.parent_controller.constantize            # "ApplicationController"
  def signed_in_resource
    warden.authenticate(scope: resource_name)   # Devise::Controllers::Helpers#warden; crg: no link
  end
end
```

Model modules are the same: `devise :confirmable, :lockable` includes `Devise::Models::Confirmable`
and the rest at run time.

**What this change does:** booting the app and asking Ruby gives the answer exactly, with no guess.
crg reads an optional dump at `<repo>/.code-review-graph/ruby-runtime.json`: `files`, the SHA-256
of each source file at boot, and `modules`: per class or module, its `kind`, its `ancestors` and the
`file:line` of each method its objects answer, as `UnboundMethod#source_location` gives it. A
method in a file whose hash no longer matches is dropped, since its line may have moved.

1. `InheritanceIndex.ruby_runtime_method` (`inheritance.py`): the method node at the dump's
   `file:line` for the class (its Ruby name, `Shop::Host`); for a module, the one method the classes
   including it resolve (none when they differ). A class's own entry is complete: its subclasses
   are no answer. Marked `crg-fork (SYN-F-637)`.
2. `GraphStore._ruby_runtime` and `_class_backed_candidates` (`graph.py`): load the dump, read
   once per version of the file and with each source file's hash cached by its modification time
   (absent: ignored; unreadable or without `files` and `modules`: ignored with a warning); a Ruby
   call on `self` that the class does not declare itself takes the dump's method before crg's
   static walk. `super`, calls in class methods and calls on other objects are not touched.
   Marked `crg-fork (SYN-F-637)`.

Without a dump, nothing changes. Making the dump is outside crg:
`lab/ruby-calls-changelog/boot-dump-scratch/boot_dump.rb` boots devise's `test/rails_app`, eager
loads it and writes the dump (1.3 s; gems installed with `BUNDLE_PATH=<scratch>/gems
BUNDLE_WITHOUT=mongoid bundle install`).

No twin: crg has no run-time input; the expected links follow the dump, written by hand.

**Tests:** `tests/test_ruby_call_follows_the_runtime_dump.py`: 15 tests, 3 fail with the change
off; the other 12 guard what must stay unlinked (no dump, a bad, non-object or hash-less dump with
its warning, a file changed since the dump or missing from its hashes, a line with no method,
includers that disagree, a subclass, a call on an object, `super`) and the class's own method coming
first; a class-method case is in `test_ruby_call_without_receiver_is_on_self.py`. 17 mutations of
this change and the class-method fix, each caught (`boot-dump-scratch/mutate.log`).

**Prototype first:** `boot-dump-scratch/proto.py` relinked a copy of the graph: 728/295/52, the same
as the built change.

## Results

Measured 2026-11-27 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1–11, **after** adds this change with devise's dump.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 657 → 728 | 366 → 295 | 50 → 52 |

- +71 right: mixin misses 92 → 27 (controllers reaching `Devise::Controllers::Helpers`, model
  modules reaching each other through `devise :…`).
- The 2 "invented" are `validatable.rb:58` `password` / `password_confirmation` → the attribute
  readers in `database_authenticatable.rb`, which Ruby does resolve; the scorer puts them in "call
  did not run on this pass". 3 rows moved to "linked to another definition": `authenticatable_salt`,
  which the tests redefine in `test/models/rememberable_test.rb`. Neither is a wrong link in the app.

**Timing:** devise build with post-processing, 2.1–2.2 s without the dump and 2.1 s with it (two
runs each).

## Known limits

- The dump describes one boot: one configuration of the app (devise's test app here). Code only
  another configuration loads is not in it.
- `super` is not answered from the dump; the 9 `super` misses into run-time model modules
  (SYN-F-638) could be, from the ancestors after the method's owner.
- Calls on other objects (`resource.save`) need the object's type; the dump does not give it.
- An app that cannot boot gets no dump, and crg falls back to the static rules.

## History

- 2026-11-27: added (SYN-F-637).
- 2026-11-27, after code review: file hashes in the dump (a changed file's methods are dropped), the
  dump and hashes cached, a warning for a bad dump, `MethodPlace` for the method-place key.
  devise unchanged at 728/295/52.
