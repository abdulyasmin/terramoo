":tmoo_sysrefs() => {{name, obj}, ...}: every $name on #0 that holds an object.";
out = {};
for p in (properties(#0))
  v = `#0.(p) ! ANY => 0';
  if (typeof(v) == OBJ)
    out = {@out, {p, v}};
  endif
endfor
return out;
