# ToastStunt testbed

ToastStunt with ToastCore on `127.0.0.1:17001` (`MOO_PORT` overrides).
`setup.sh`, `start.sh` and `stop.sh` say what they do at the top.

- `setup.sh` clones ToastStunt and ToastCore from github.com/lisdude into
  `.src/` (`TOASTSTUNT_SRC` / `TOASTCORE_DB` point elsewhere) and installs
  missing Homebrew dependencies.
- The DB is prepared by piping `prepare.moo` into emergency wizard mode
  (`moo -e`). Logins: `tester`/`tester` (programmer, not wizard) and
  `wiz`/`wiz`; the stock `Wizard` (#2, no password) remains.
- `;expr` prints `=> value` (objects as `#N  (name)`), `;;stmts` runs
  statements. The login screen sends telnet `IAC WILL 70`.
