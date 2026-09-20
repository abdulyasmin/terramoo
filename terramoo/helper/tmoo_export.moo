":tmoo_export(objs) => one record per object, for the terramoo exporter.";
"Values come back as toliteral() text so nothing is lost in the gate's JSON.";
"Inherited properties are listed only when set on the object (not clear).";
out = {};
for o in (args[1])
  if (!valid(o))
    out = {@out, ["obj" -> o, "error" -> "invalid"]};
    continue;
  endif
  props = {};
  for p in (properties(o))
    info = `property_info(o, p) ! ANY => {#-1, "?"}';
    props = {@props, ["name" -> p, "defined" -> 1, "owner" -> info[1], "perms" -> info[2], "value" -> toliteral(`o.(p) ! ANY => E_PERM')]};
  endfor
  for a in (ancestors(o))
    for p in (properties(a))
      if (!`is_clear_property(o, p) ! ANY => 1')
        info = `property_info(o, p) ! ANY => {#-1, "?"}';
        props = {@props, ["name" -> p, "defined" -> 0, "owner" -> info[1], "perms" -> info[2], "value" -> toliteral(`o.(p) ! ANY => E_PERM')]};
      endif
    endfor
  endfor
  vrbs = {};
  i = 0;
  for vname in (verbs(o))
    i = i + 1;
    info = `verb_info(o, i) ! ANY => {#-1, "?", vname}';
    vargs = `verb_args(o, i) ! ANY => {"?", "?", "?"}';
    code = `verb_code(o, i) ! ANY => {}';
    vrbs = {@vrbs, ["names" -> info[3], "owner" -> info[1], "perms" -> info[2], "args" -> vargs, "code" -> code]};
  endfor
  flags = tostr(o.r ? "r" | "", o.w ? "w" | "", o.f ? "f" | "");
  out = {@out, ["obj" -> o, "name" -> o.name, "parent" -> parent(o), "location" -> o.location, "owner" -> o.owner, "flags" -> flags, "props" -> props, "verbs" -> vrbs]};
endfor
return out;
