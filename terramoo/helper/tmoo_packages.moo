":tmoo_packages(mode, ...) maintains package ownership and a deployment CAS token.";
"State is {schema, world id, epoch, {key, generation, installation id, desired} records}.";
"This helper never suspends and accepts only the toolbox owner's permissions.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
mode = args[1];
present = "_terramoo_package_state" in properties(this);
state = present ? this._terramoo_package_state | {1, "", "", {}};
if (typeof(state) != typeof({}) || length(state) != 4 || state[1] != 1 || typeof(state[2]) != typeof("") || typeof(state[3]) != typeof("") || typeof(state[4]) != typeof({}))
  raise(E_INVARG, "malformed package ownership state");
endif
if (mode == "bootstrap")
  if (!present)
    add_property(this, "_terramoo_package_state", state, {this.owner, "r"});
  endif
  return state;
elseif (mode == "read")
  return state;
elseif (mode == "begin" || mode == "import")
  world_id = args[2];
  expected = args[3];
  epoch = args[4];
  entries = args[5];
  if (state[3] != expected || (state[4] && state[2] != world_id))
    raise(E_INVARG, "package deployment revision changed; recover or refresh the matching checkout");
  endif
  if (typeof(world_id) != typeof("") || !world_id || typeof(epoch) != typeof("") || !epoch || epoch == expected || typeof(entries) != typeof({}))
    raise(E_INVARG, "invalid package deployment identity");
  endif
  reg = this.registry;
  keys = {};
  for entry in (entries)
    if (typeof(entry) != typeof({}) || length(entry) != 4 || typeof(entry[1]) != typeof("") || !entry[1] || typeof(entry[2]) != typeof("") || !entry[2] || typeof(entry[3]) != typeof("") || !entry[3] || (entry[4] != 0 && entry[4] != 1))
      raise(E_INVARG, "invalid package ownership entry");
    endif
    if (entry[1] in keys)
      raise(E_INVARG, "duplicate package ownership key");
    endif
    keys = {@keys, entry[1]};
    i = entry[1] in reg[1];
    if (i && valid(reg[2][i]))
      if (reg[3][i] != entry[2] || this:tmoo_generation("read", reg[2][i]) != entry[2])
        raise(E_INVARG, tostr("package generation changed: ", entry[1]));
      endif
    endif
  endfor
  for old in (state[4])
    i = old[1] in keys;
    registered = old[1] in reg[1];
    if (i)
      if (entries[i][3] != old[3] && !(mode == "import" && old[3] == tostr("local:", world_id) && entries[i][2] == old[2]))
        raise(E_INVARG, tostr("installation ownership changed: ", old[1]));
      endif
    elseif (registered)
      raise(E_INVARG, tostr("cannot release a registered owned key: ", old[1]));
    endif
  endfor
  state = {1, world_id, epoch, entries};
  if (present)
    this._terramoo_package_state = state;
  else
    add_property(this, "_terramoo_package_state", state, {this.owner, "r"});
  endif
  return state;
elseif (mode == "check")
  epoch = args[2];
  op = args[3];
  if (epoch && epoch != state[3])
    raise(E_INVARG, "stale package deployment token");
  endif
  kind = op[1];
  if (kind == "link")
    bindings = op[2];
  elseif (kind == "rename")
    reg = this.registry;
    bindings = {{reg[1][op[2]], op[4], op[5]}};
    destination = op[6];
    for entry in (state[4])
      if (entry[1] == destination && (entry[1] != reg[1][op[2]] || !epoch || entry[2] != op[5]))
        raise(E_INVARG, "rename destination belongs to another installation");
      endif
    endfor
  elseif (kind == "create")
    bindings = {{op[2], #-1, op[7]}};
  elseif (kind == "register")
    bindings = {{op[2], op[3], op[4]}};
  else
    bindings = {{op[2], op[3], op[4]}};
  endif
  for binding in (bindings)
    for entry in (state[4])
      if (entry[1] == binding[1])
        if (!epoch || entry[2] != binding[3])
          raise(E_INVARG, tostr("package ownership requires the current deployment: ", entry[1]));
        endif
        if (kind == "destroy" || kind == "unregister")
          if (entry[4])
            raise(E_INVARG, tostr("owned object is still desired: ", entry[1]));
          endif
        elseif (kind != "rename" && !entry[4])
          raise(E_INVARG, tostr("owned object is pending removal: ", entry[1]));
        endif
      endif
    endfor
  endfor
  return 1;
else
  raise(E_INVARG, "unknown package ownership operation");
endif
