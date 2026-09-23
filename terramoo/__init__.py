"""terramoo: a MOO player's objects as files, on any MOO.

`tmoo pull` reads the objects the registry names into objdef files,
`tmoo plan` diffs the files against the live MOO, `tmoo apply` makes the MOO
match the files.  Everything runs as the player -- logged in over telnet
or TLS, or through a hosted MCP gate -- so nothing here can do what the
player could not do from `;` eval.
"""

__version__ = "0.2.0"
