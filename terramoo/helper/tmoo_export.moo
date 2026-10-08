":tmoo_export(bindings [, may_suspend]) => one record per {key, object, generation}, for terramoo's exporter:";
"  {obj, name, parent, location, owner, flags, props, verbs}, or {obj} for an invalid one.";
"  props: {{name, defined, owner, perms, toliteral(value)}, ...}; inherited ones only when set here.";
"  verbs: {{names, owner, perms, {dobj, prep, iobj}, code, numeric descriptor}, ...}.";
"Plain LambdaMOO 1.8 -- no maps, ancestors(), core utilities or type constants (mooR spells them TYPE_OBJ) -- so it runs on every server.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
bindings = args[1];
may_suspend = length(args) > 1 && args[2];
out = {};
for binding in (bindings)
  key = binding[1];
  o = binding[2];
  nonce = binding[3];
  reg = this.registry;
  if (length(reg) == 2)
    nonces = {};
    for ignored in (reg[1])
      nonces = {@nonces, ""};
    endfor
    reg = {reg[1], reg[2], nonces};
  endif
  i = key in reg[1];
  if (!i || reg[2][i] != o)
    raise(E_INVARG, tostr(key, ": registry binding changed before export"));
  endif
  if (reg[3][i] != nonce)
    raise(E_INVARG, tostr(key, ": registry generation changed before export"));
  endif
  if (!valid(o))
    out = {@out, {o}};
    continue;
  endif
  if (!nonce)
    raise(E_INVARG, tostr(key, ": unverified legacy binding; confirm it with tmoo adopt ", o, " ", key, " --verify"));
  endif
  if (this:tmoo_generation("read", o) != nonce)
    raise(E_INVARG, tostr(key, ": object generation does not match the registry"));
  endif
  props = {};
  for p in (properties(o))
    info = `property_info(o, p) ! ANY => {#-1, "?"}';
    props = {@props, {p, 1, info[1], info[2], toliteral(`o.(p) ! ANY => E_PERM')}};
  endfor
  a = parent(o);
  while (valid(a))
    for p in (properties(a))
      if (!`is_clear_property(o, p) ! ANY => 1')
        info = `property_info(o, p) ! ANY => {#-1, "?"}';
        props = {@props, {p, 0, info[1], info[2], toliteral(`o.(p) ! ANY => E_PERM')}};
      endif
    endfor
    a = parent(a);
  endwhile
  vrbs = {};
  i = 0;
  for vname in (verbs(o))
    i = i + 1;
    info = `verb_info(o, i) ! ANY => {#-1, "?", vname}';
    vargs = `verb_args(o, i) ! ANY => {"?", "?", "?"}';
    code = `verb_code(o, i) ! ANY => {}';
    vrbs = {@vrbs, {info[3], info[1], info[2], vargs, code, i}};
  endfor
  flags = tostr(o.r ? "r" | "", o.w ? "w" | "", o.f ? "f" | "");
  out = {@out, {o, o.name, parent(o), o.location, o.owner, flags, props, vrbs}};
  if (may_suspend && (ticks_left() < 10000 || seconds_left() < 2))
    suspend(0);
  endif
endfor
return out;
