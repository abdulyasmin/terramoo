"""Prepare a pristine LambdaCore for tests (run by setup.sh, not by hand).

Connects to a freshly loaded LambdaCore as the passwordless Wizard, sets
the wizard password, makes the non-wizard programmer `tester`, gives it a
generous quota, then calls shutdown() so the server writes its output DB.
"""

import argparse
import re
import socket
import sys
import time


class Moo:
    def __init__(self, port: int, wait: float = 60.0):
        deadline = time.time() + wait
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.sock.settimeout(0.2)
        self.read()

    def read(self, idle: float = 0.6, limit: float = 30.0) -> str:
        out, end, stop = b"", time.time() + idle, time.time() + limit
        while time.time() < min(end, stop):
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            out += data
            end = time.time() + idle
        return out.decode("latin-1")

    def send(self, line: str) -> str:
        self.sock.sendall(line.encode() + b"\r\n")
        return self.read()

    def eval(self, expr: str) -> str:
        """Evaluate `;expr`; return the value text after `=> `, or die."""
        out = self.send(";" + expr)
        m = re.search(r"^=> (.*)$", out, re.M)
        if not m or "(End of traceback)" in out:
            sys.exit(f"prepare_db: `;{expr}` failed:\n{out}")
        print(f";{expr}\n  => {m.group(1)}")
        return m.group(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=17002)
    port = ap.parse_args().port

    moo = Moo(port)
    out = moo.send("connect Wizard")
    if "*** Connected ***" not in out:
        sys.exit(f"prepare_db: could not connect as Wizard:\n{out}")

    # Wizard: password `wiz`, and `wiz` as an alias so `connect wiz wiz` works.
    moo.eval('#2.password = crypt("wiz")')
    moo.eval('#2.aliases = {"Wizard", "wiz"}')
    moo.eval('$player_db:insert("wiz", #2)')

    # tester: an ordinary player, then a programmer (chparents to $prog).
    moo.eval('$wiz_utils:make_player("tester", "tester@localhost")')
    tester = moo.eval('$player_db:find_exact("tester")')
    if not re.fullmatch(r"#\d+", tester.split()[0]):
        sys.exit(f"prepare_db: tester lookup gave {tester}")
    tester = tester.split()[0]
    moo.eval(f'{tester}.password = crypt("tester")')
    moo.eval(f"$wiz_utils:set_programmer({tester})")

    # Quota, two layers:
    # - The server's create() builtin charges `.ownership_quota` itself and
    #   raises E_QUOTA at <= 0.  LambdaCore's byte-quota setup parks it at a
    #   large negative number, so give it a large positive one.
    # - LambdaCore's @create/$quota_utils path is byte-based: 100 MB, and lift
    #   the "objects not yet measured" cap (default 10).
    moo.eval(f"{tester}.ownership_quota = 1000000")
    moo.eval(f"$quota_utils:set_quota({tester}, 100000000)")
    moo.eval("$quota_utils.max_unmeasured = 1000000")
    moo.eval(
        f"{{{tester}.programmer, {tester}.wizard, "
        f"{tester}.ownership_quota, {tester}.size_quota}}"
    )

    # shutdown() makes the server dump to its output DB and exit.
    moo.sock.sendall(b';shutdown("prepare_db done")\r\n')
    moo.read(idle=2.0)
    print("prepare_db: done")


if __name__ == "__main__":
    main()
