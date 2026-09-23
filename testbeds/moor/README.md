# mooR testbed

A local [mooR](https://github.com/rdaum/moor) 1.0.2 server running the bundled `lambda-moor` core (LambdaCore 2018), for integration tests.

- `./setup.sh` clones mooR at the pinned tag into `.build/`, builds `moor-daemon`, `moor-telnet-host` and `moor-web-host` with cargo, and imports the core into `.run/`. It then provisions the logins. Re-running it does nothing if the setup is already done. `./setup.sh --reset` rebuilds the world from scratch.
- `./start.sh` / `./stop.sh` run the three processes in the background. The pidfiles are `.run/*.pid` and the logs are in `.run/logs/`.
- Telnet is `127.0.0.1:17003` (health check `:17988`). The HTTP API is `127.0.0.1:17080`. The processes talk to each other over ZeroMQ IPC sockets in `.run/ipc/`, so no CURVE keys or enrollment are needed.
- Logins: `connect tester tester` is a non-wizard programmer (#98, child of `$prog`). `connect Wizard wizard` is the wizard (#2).
- `;expr` evaluates an expression and prints `=> value` (objects look like `#99  (name)`). `;;stmts` runs statements. Compile errors print a message and then `N error(s).`. Runtime errors print a traceback ending in `Task exception: ...`.
- `moo_client.py` is a stdlib client that wraps each command in `PREFIX`/`SUFFIX` markers. `probe.py` records what tester can do. `provision.py` creates the logins.
- `moor.yaml` holds the daemon's feature flags, which are set to mooR's LambdaMOO-compatible defaults: numbered objects and 0/1 returns. Turn on `use_uuobjids` to test `#048D05-1234567890`-style UUID objects, then run `./setup.sh --reset`.
- HTTP: `POST /auth/connect` (form `player`, `password`) returns an `x-moor-auth-token` header. Send that token as `X-Moor-Auth-Token` to `POST /v1/eval`, which takes a program body such as `return 1;` and answers JSON when you send `Accept: application/json`. Other endpoints: `GET /v1/verbs/oid:98`, `/v1/properties/oid:98`, `/v1/objects`, `/health`, `/version`, `/openapi.yaml`.
- mooR is pinned to tag 1.0.2 and not to `main`: current `main` requires the `x` flag on command verbs, which breaks `;` and most LambdaCore commands for non-wizards.
