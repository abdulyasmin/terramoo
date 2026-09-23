":tmoo_info(objs) => {{obj, valid, name}, ...}, and with no objects, what this player owns:";
"  {owned objects or E_PROPNF when the core keeps no list, server_version()}.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
if (!args)
  return {`this.owner.owned_objects ! ANY => E_PROPNF', `server_version() ! ANY => ""'};
endif
out = {};
for o in (args[1])
  out = {@out, {o, valid(o), valid(o) ? `o.name ! ANY => ""' | ""}};
endfor
return out;
