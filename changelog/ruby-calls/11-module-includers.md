# module-includers (include marks in class-parents, includer lookup in inheritance.py / graph.py)

**What crg does wrong:** in a Ruby module's method, `self` is an object of a class that includes
the module, so a module may call a method that only its includers declare. crg looks a call on
`self` up in the caller's class and its ancestors only, so such a call stays unlinked
(SYN-F-636):

```ruby
module Devise::Controllers::Rememberable
  def forget_cookie(scope)
    cookies.delete(remember_key(self, scope))   # Devise::Hooks::Proxy includes Rememberable
  end                                           # and delegates `cookies`; crg: no link
end
```

**What this change does** (three parts):

1. Rule `class-parents` marks the INHERITS edge to an included module with
   `extra["ruby_include"] = True`, so the index can tell a module's includers from a class's
   subclasses (a superclass's object is not a subclass's).
2. `InheritanceIndex.ruby_includer_method` (`inheritance.py`): the one method of that name that the
   classes and modules including the module declare; none when no includer declares it or two
   declare different ones. Marked `crg-fork (SYN-F-636)`.
3. `GraphStore._class_backed_candidates` (`graph.py`): a Ruby call on `self` that the caller's
   class and its ancestors do not answer is looked up in the includers. `super` is not: it looks
   up, and an includer is below the module. Marked `crg-fork (SYN-F-636)`.

No twin: crg writes no Ruby parents; the expected links are written by hand.

**Other tests:** change 8's tag test now expects the `ruby_include` key on the include edge.

**Tests:** `tests/test_ruby_module_call_reaches_its_includer.py`: 7 tests, 2 fail with the change
off; the other 5 guard what must stay unlinked (two includers that disagree, a subclass, a call on
an object, `super`) and the module's own ancestors coming first. 7 mutations of the three parts,
each caught (`lab/ruby-calls-changelog/module-includers-scratch/mutate.log`).

**Prototypes measured first** (on changes 1–10, 651/372/50; scripts in
`lab/ruby-calls-changelog/ancestor-order-scratch/`):

- Any still-unlinked call on `self` linked to the one module method of that name anywhere in the
  graph: 701/322/**112**, +50 right but +62 invented (Rails' `request`, `respond_to?`, devise's
  config readers). Rejected.
- The includers alone: 657/366/50. The includers plus the other modules they include (siblings):
  the same. Built: the includers alone.

## Results

Measured 2026-11-27 on crg 2.4.0, devise, against the calls devise's test suite really makes
(1,023). **before** is crg 2.4.0 with changes 1–10, **after** adds this change.

| repo | real calls | right (before → after) | missed (before → after) | invented (before → after) |
|---|---:|---:|---:|---:|
| devise | 1,023 | 651 → 657 | 372 → 366 | 50 → 50 |

No right link is lost and nothing is invented; the rows match the prototype's.

- +6 right: 4 calls in `Devise::Controllers::Rememberable` and 1 in `SignInOut` to the methods
  `Devise::Hooks::Proxy` (which includes both) delegates (`cookies`, `request`, `session`), and
  `sign_in_out.rb:90` → `Helpers#expire_data_after_sign_out!` (`Helpers` includes `SignInOut`).
- Most mixin misses stay: the controllers call `Devise::Controllers::Helpers`, which Rails includes
  at run time (`ActiveSupport.on_load(:action_controller) { include … }`), and the model modules
  are combined at run time by `devise :database_authenticatable, …`. `SignInOut`'s 11 calls to
  `warden` stay unlinked: both its includers declare one (`Helpers#warden`, `Proxy`'s
  `attr_reader :warden`), and which runs depends on the object.

**Timing:** the lookup runs after the parse; the devise build with post-processing takes 2.1 s
with the lookup off and 2.1 s with it on (two runs each).

## Known limits

- An includer's own ancestors are not searched: only methods the includer declares.
- Modules included at run time (`on_load` blocks, `devise :…`) are not includers.

## History

- 2026-11-27: added (SYN-F-636).
