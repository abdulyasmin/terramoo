"""Give the fresh LambdaCore world its test logins (run by setup.sh).

- Wizard (#2): password "wizard" (LambdaCore ships it with none).
- tester: a normal non-wizard player with the programmer bit, password
  "tester", made the LambdaCore way ($wiz_utils:make_player + set_programmer,
  so it is a child of $prog with programmer quota).

Idempotent: re-running leaves an already-provisioned world alone.
"""

import sys

from moo_client import Moo

m = Moo()
banner = m.drain(1.0)
for pw in ("wizard", ""):
    m.send(f"connect Wizard {pw}".rstrip())
    text = m.drain(1.5)
    if "*** Connected ***" in text:
        break
else:
    sys.exit(f"cannot log in as Wizard:\n{banner}\n{text}")

print(*m.eval('#2.password = argon2("wizard", salt())'), sep="\n")

code = (
    ';p = $string_utils:match_player("tester"); '
    'if (!valid(p)) '
    '  r = $wiz_utils:make_player("tester", "tester@localhost"); p = r[1]; '
    'endif '
    'p.password = argon2("tester", salt()); '
    'p.description = "A test programmer for terramoo."; '
    '$wiz_utils:set_programmer(p); '
    'return {p, p.name, is_player(p), p.programmer, p.wizard, parent(p)};'
)
out = m.eval(code)
print(*out, sep="\n")
m.close()
if not any('"tester"' in line for line in out):
    sys.exit("provisioning tester failed")
