"""terramoo: a MOO player's objects as files.

`tmoo export` reads the objects the registry names into objdef files,
`tmoo plan` diffs the files against the live MOO, `tmoo apply` makes the MOO
match the files.  Everything talks to the MOO through the hosted MCP server
as the player, so nothing here can do what the player could not do from
`eval`.
"""

__version__ = "0.1.0"
