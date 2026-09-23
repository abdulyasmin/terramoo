# ToastStunt testbed

A local ToastStunt server (ToastCore DB) for terramoo's integration tests, built from
source with no Docker. It listens on plain telnet at `127.0.0.1:17001` (`MOO_PORT` overrides).

- `./setup.sh` clones ToastStunt and ToastCore from github.com/lisdude into `.src/`, builds
  the server out of tree into `.build/` (override the source with `TOASTSTUNT_SRC`; installs
  missing brew deps) and prepares `.run/base.db` from a copy of `toastcore.db`
  (`TOASTCORE_DB`). Idempotent; `--reset` re-prepares.
- The DB is prepared by piping `prepare.moo` into the server's emergency wizard mode (`moo -e`),
  which adds `tester`/`tester` (programmer, not wizard) and `wiz`/`wiz` (wizard), both with
  `ownership_quota = 100000`. The stock `Wizard` (#2, `connect wizard`, no password) remains.
- `./start.sh` runs the server in the background (pid `.run/moo.pid`, log `.run/moo.log`),
  on `.run/moo.db`; `./start.sh --fresh` first resets it from `base.db`.
- `./stop.sh` sends SIGTERM: the server dumps to `.run/moo.db.new`, which the next start adopts.

Talking to it: `connect tester tester`, then `;expr` prints `=> value` (objects as `#N  (name)`),
`;;stmts` runs statements, errors print a traceback with no `=>` line. `PREFIX`/`SUFFIX` and
`OUTPUTPREFIX`/`OUTPUTSUFFIX` work. The login screen sends telnet `IAC WILL 70` bytes; strip them.
