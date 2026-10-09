"Check/apply one selected player field. All operations require an exact read snapshot.";
if (caller_perms() != this.owner || player != this.owner || !is_player(this.owner))
  raise(E_PERM);
endif
mode = args[1];
selector = args[2];
expected = args[3];
action = args[4];
o = this.owner;
kind = selector[1];
name = selector[2];
current = this:tmoo_player_read("field", selector);
if (!equal(current, expected))
  raise(E_INVARG, "player field changed; pull or reconcile before applying");
endif
operation = action[1];
setter = "";
if (kind == "property")
  if (operation == "set")
    direct = action[2];
    if (current[1] && current[2] != direct || !current[1] && !direct)
      raise(E_INVARG, "property definition/override does not match the player");
    endif
    if (name == "description")
      setter = "set_description";
    endif
  elseif (operation == "remove" || operation == "clear")
    if (!current[1] || (operation == "remove" ? !current[2] | current[2]))
      raise(E_INVARG, "remove requires a local property; clear requires an inherited override");
    endif
  else
    raise(E_INVARG, "invalid player property operation");
  endif
elseif (kind == "verb")
  if (!(operation in {"set", "remove"}) || operation == "remove" && !current[1])
    raise(E_INVARG, "invalid player verb operation");
  endif
elseif (kind == "feature")
  if (!(operation in {"set", "detach"}))
    raise(E_INVARG, "invalid feature operation");
  endif
  setter = operation == "detach" ? "remove_feature" | "add_feature";
elseif (kind == "setting" && operation == "set")
  if (name == "aliases" && !(o.name in action[2]))
    raise(E_INVARG, "aliases must include the current player name");
  endif
  colon = index(name, ":");
  if (colon)
    prefix = name[1..colon - 1];
    option = name[colon + 1..length(name)];
    setter = tostr("set_", prefix, "_option");
  else
    setter = name == "linelen" ? "set_linelength" | (name == "pagelen" ? "set_pagelength" | tostr("set_", name));
  endif
else
  raise(E_INVARG, "invalid player operation");
endif
if (setter)
  a = o;
  available = 0;
  while (valid(a) && !available)
    info = `verb_info(a, setter) ! E_VERBNF => 0';
    if (info && index(info[2], "x"))
      available = 1;
    endif
    a = parent(a);
  endwhile
  if (!available)
    raise(E_VERBNF, tostr("unsupported core adapter: ", setter));
  endif
endif
if (mode == "check")
  return 1;
elseif (mode != "apply" || caller != this || this._terramoo_player_state[4] != args[5] || this._terramoo_player_state[6] != "running")
  raise(E_PERM);
endif
if (kind == "property")
  if (operation == "remove")
    delete_property(o, name);
  elseif (operation == "clear")
    clear_property(o, name);
  elseif (!current[1])
    add_property(o, name, action[3], {o, action[4]});
  else
    if (setter)
      result = o:(setter)(action[3]);
      if (typeof(result) == typeof(E_PERM))
        raise(result, "player setter refused change");
      endif
    else
      o.(name) = action[3];
    endif
    if (action[2])
      set_property_info(o, name, {o, action[4]});
    endif
  endif
elseif (kind == "verb")
  if (operation == "remove")
    delete_verb(o, current[2]);
  else
    if (current[1])
      n = current[2];
    else
      add_verb(o, {o, action[2], name}, action[3]);
      n = length(verbs(o));
    endif
    errors = set_verb_code(o, n, action[4]);
    if (errors)
      if (!current[1])
        delete_verb(o, n);
      endif
      raise(E_INVARG, tostr("compile error: ", toliteral(errors)));
    endif
    set_verb_args(o, n, action[3]);
    set_verb_info(o, n, {o, action[2], name});
  endif
elseif (kind == "feature")
  if (current[2] != (operation == "detach" ? 0 | 1))
    result = o:(setter)(name);
    if (typeof(result) == typeof(E_PERM))
      raise(result, "feature setter refused change");
    endif
  endif
elseif (kind == "setting")
  if (colon)
    result = o:(setter)(option, action[2]);
    if (typeof(result) == typeof(""))
      raise(E_INVARG, result);
    endif
  elseif (name == "linelen")
    result = o:(setter)(action[2], 1);
  else
    result = o:(setter)(action[2]);
  endif
  "set_gender may return E_NONE as a successful custom-pronoun result.";
  if (typeof(result) == typeof(E_PERM) && !(name == "gender" && result == E_NONE))
    raise(result, "player setter refused change");
  endif
endif
return this:tmoo_player_read("field", selector);
