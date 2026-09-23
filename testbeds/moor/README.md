# mooR testbed

[mooR](https://github.com/rdaum/moor) 1.0.2 with its bundled `lambda-moor`
core (LambdaCore 2018), telnet on `127.0.0.1:17003`. `setup.sh`, `start.sh`
and `stop.sh` say what they do at the top.

- Logins: `connect tester tester` (non-wizard programmer, #98) and
  `connect Wizard wizard` (#2).
- Pinned to 1.0.2, not `main`: on `main` command verbs need the `x` flag,
  which breaks `;` and most LambdaCore commands for non-wizards.
- `;expr` prints `=> value` (objects as `#99  (name)`), `;;stmts` runs
  statements. A compile error prints its message, then `N error(s).`
- `moor.yaml` holds mooR's LambdaMOO-compatible feature flags. Turn on
  `use_uuobjids` and run `./setup.sh --reset` to test UUID objects
  (`#048D05-1234567890`).
- The web host (HTTP on `127.0.0.1:17080`, see `/openapi.yaml`) runs too;
  terramoo doesn't use it.
- `provision.py` creates the logins through `moo_client.py`, a stdlib client
  that brackets each command with `PREFIX`/`SUFFIX`.
