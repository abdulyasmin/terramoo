# LambdaMOO testbed

Classic LambdaMOO 1.8.1 (no ToastStunt extensions) with LambdaCore-17May04, for
integration tests over telnet. `.build/` and `.run/` are gitignored.

- `./setup.sh` fetches the pinned server tarball and core (sha256-checked), builds
  `.build/moo`, and prepares `.run/moo.db` by scripting a Wizard session
  (`prepare_db.py`). Re-running is a no-op; `./setup.sh --reset` rebuilds the DB.
- `./start.sh` / `./stop.sh` run it in the background on `127.0.0.1:17002`
  (`MOO_PORT` overrides); pidfile `.run/moo.pid`, log `.run/moo.log`. Stopping
  dumps to `moo.db.new`, which the next start promotes; `stop.sh --discard` drops it.
- Players: `connect tester tester` (programmer, not wizard, quota 1e6 objects /
  100 MB) and `connect wiz wiz` (= Wizard #2).
- Build notes: configure's ANSI probe is patched for modern clang, the code is
  compiled `-std=gnu89 -w`, and the listener is patched to bind loopback only.
- Classic 1.8.1 limits: no maps, no `ancestors()`/`isa()`/`error()` builtins,
  32-bit integers, no int/float mixing. Use `$object_utils:ancestors/isa`.
- LambdaCore eval: `;expr` evaluates one expression (`return` is prepended);
  `;;stmts` or a line starting with a statement keyword runs statements.
