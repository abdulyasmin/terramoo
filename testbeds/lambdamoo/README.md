# LambdaMOO testbed

Classic LambdaMOO 1.8.1 with LambdaCore-17May04 on `127.0.0.1:17002`
(`MOO_PORT` overrides). `setup.sh`, `start.sh` and `stop.sh` say what they
do at the top.

- Logins: `connect tester tester` (programmer, not wizard) and
  `connect wiz wiz` (Wizard, #2).
- The server tarball and core are sha256-checked. `setup.sh` patches
  configure for modern clang and binds the listener to loopback only.
- 1.8.1 has no maps, no `ancestors()`/`isa()`/`error()`, 32-bit integers
  and no int/float mixing: the limits the helpers are written to.
- `;expr` evaluates an expression, `;;stmts` runs statements.
