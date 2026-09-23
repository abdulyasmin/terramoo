":tmoo_sysrefs() => {{name, obj}, ...}: every $name on #0 that holds an object.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
out = {};
for p in (properties(#0))
  v = `#0.(p) ! ANY => 0';
  if (typeof(v) == typeof(#0))
    out = {@out, {p, v}};
  endif
endfor
return out;
