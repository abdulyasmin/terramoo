":tmoo_export(objs [, may_suspend]) => one record per object, for terramoo's exporter:";
"  {obj, name, parent, location, owner, flags, props, verbs}, or {obj} for an invalid one.";
"  props: {{name, defined, owner, perms, toliteral(value)}, ...}; inherited ones only when set here.";
"  verbs: {{names, owner, perms, {dobj, prep, iobj}, code}, ...}.";
"Plain LambdaMOO 1.8 -- no maps, ancestors(), core utilities or type constants (mooR spells them TYPE_OBJ) -- so it runs on every server.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
objs = args[1];
may_suspend = length(args) > 1 && args[2];
out = {};
for o in (objs)
  if (!valid(o))
    out = {@out, {o}};
    continue;
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
    vrbs = {@vrbs, {info[3], info[1], info[2], vargs, code}};
  endfor
  flags = tostr(o.r ? "r" | "", o.w ? "w" | "", o.f ? "f" | "");
  out = {@out, {o, o.name, parent(o), o.location, o.owner, flags, props, vrbs}};
  if (may_suspend && (ticks_left() < 10000 || seconds_left() < 2))
    suspend(0);
  endif
endfor
return out;
