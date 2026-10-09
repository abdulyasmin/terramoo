"Selective player reads. Protected names are rejected before any value read.";
if (caller_perms() != this.owner || player != this.owner || !is_player(this.owner))
  raise(E_PERM);
endif
o = this.owner;
mode = args[1];
denied = __TMOO_PLAYER_PROTECTED__;
if (mode == "inspect")
  props = {};
  for name in (properties(o))
    if (!(name in denied) && !index(name, "password") && !index(name, "secret") && !index(name, "token") && !index(name, "credential") && index(name, "_terramoo_") != 1)
      props = {@props, {name, property_info(o, name)}};
    endif
  endfor
  vrbs = {};
  for i in [1..length(verbs(o))]
    vrbs = {@vrbs, {i, verb_info(o, i), verb_args(o, i)}};
  endfor
  return {o, o.owner, o.programmer, o.wizard, parent(o), props, vrbs};
elseif (mode == "features")
  try
    return o.features;
  except (E_PROPNF)
    return {};
  endtry
endif
selector = args[2];
kind = selector[1];
name = selector[2];
if (kind == "property")
  if (name in denied || index(name, "password") || index(name, "secret") || index(name, "token") || index(name, "credential") || index(name, "_terramoo_") == 1)
    raise(E_PERM, "protected player property");
  endif
  try
    info = property_info(o, name);
  except (E_PROPNF)
    return {0};
  endtry
  if (info[1] != o)
    raise(E_PERM, "player properties must be owned by the player; use a supported setting");
  endif
  direct = name in properties(o) ? 1 | 0;
  clear = is_clear_property(o, name);
  return {1, direct, clear, info[1], info[2], o.(name)};
elseif (kind == "verb")
  found = 0;
  names = verbs(o);
  for i in [1..length(names)]
    "Exact full specifications identify a local verb. Never follow inheritance.";
    if (names[i] == name)
      if (found)
        raise(E_INVARG, "ambiguous duplicate local verb specification");
      endif
      found = i;
    endif
  endfor
  if (!found)
    remaining = name;
    while (remaining)
      space = index(remaining, " ");
      alias = space ? remaining[1..space - 1] | remaining;
      remaining = space ? remaining[space + 1..length(remaining)] | "";
      if (!alias)
        continue;
      endif
      star = index(alias, "*");
      probes = {alias};
      if (star)
        probes = {alias[1..star - 1] + alias[star + 1..length(alias)]};
        if (star > 1)
          probes = {@probes, alias[1..star - 1]};
        endif
      endif
      for probe in (probes)
        if (`verb_info(o, probe) ! E_VERBNF => 0')
          raise(E_INVARG, "a local verb already uses this alias; track its full specification or remove it explicitly first");
        endif
      endfor
    endwhile
    return {0};
  endif
  info = verb_info(o, found);
  if (info[1] != o)
    raise(E_PERM, "player verbs must be owned by the player");
  endif
  return {1, found, info[1], info[2], info[3], verb_args(o, found), verb_code(o, found)};
elseif (kind == "feature")
  if (typeof(name) != typeof(#0) || !valid(name) || name == o || name == this)
    raise(E_INVARG, "invalid feature object");
  endif
  return {1, name in o.features ? 1 | 0};
elseif (kind == "setting")
  colon = index(name, ":");
  if (colon)
    prefix = name[1..colon - 1];
    option = name[colon + 1..length(name)];
    if (!(prefix in {"display", "edit", "prog", "build"}) || !option)
      raise(E_INVARG, "unsupported player setting");
    endif
    prop = tostr(prefix, "_options");
    info = property_info(o, prop);
    value = o:(tostr(prefix, "_option"))(option);
    "Only the selected option is read. Other options are not managed.";
    if (typeof(value) == typeof(E_PERM))
      raise(value, "cannot read player option");
    endif
    return {1, value, {info}};
  elseif (name in {"aliases", "gender", "home", "linelen", "pagelen"})
    info = property_info(o, name);
    guard = {info, is_clear_property(o, name)};
    if (name == "gender")
      for prop in ({"ps", "psc", "po", "poc", "pp", "ppc", "pq", "pqc", "pr", "prc"})
        guard = {@guard, {prop, property_info(o, prop), is_clear_property(o, prop), o.(prop)}};
      endfor
    endif
    return {1, o.(name), guard};
  endif
endif
raise(E_INVARG, "unsupported player field");
