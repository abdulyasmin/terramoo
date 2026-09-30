":tmoo_generation(\"read\", object) => the object's own generation stamp, or \"\" when it has none.";
":tmoo_generation(\"stamp\", object, nonce) gives the object its own stamp: the property may be new here, inherited, or defined by a descendant.";
":tmoo_generation(\"chparent\", object, parent) reparents the object and keeps every stamp in its subtree.";
":tmoo_generation(\"managed\", object) => {{object, nonce}, ...}: the registry bindings strictly below the object.";
":tmoo_generation(\"restamp\", bindings) puts back each of those stamps that is missing, e.g. after recycling their ancestor.";
"A stamp is a non-clear value of _terramoo_generation. A hierarchy defines a property once, so a managed";
"child sets the value it inherits, and a managed ancestor takes over a descendant's definition.";
"Plain LambdaMOO 1.8: no descendants(), so the subtree is walked with children() only when a conflict needs it.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
mode = args[1];
o = args[2];
prop = "_terramoo_generation";
info = {this.owner, "r"};
if (mode == "read")
  if (!valid(o) || `is_clear_property(o, prop) ! ANY => 1')
    return "";
  endif
  return `o.(prop) ! ANY => ""';
elseif (mode == "stamps")
  "Every own stamp below (not on) the object, parents before children: {{object, nonce}, ...}.";
  out = {};
  queue = children(o);
  while (queue)
    d = queue[1];
    queue = listdelete(queue, 1);
    if (valid(d))
      if (!`is_clear_property(d, prop) ! ANY => 1')
        out = {@out, {d, `d.(prop) ! ANY => ""'}};
      endif
      queue = {@queue, @children(d)};
    endif
  endwhile
  return out;
elseif (mode == "stamp")
  nonce = args[3];
  if (`property_info(o, prop) ! E_PROPNF => 0')
    o.(prop) = nonce;
    set_property_info(o, prop, info);
    return nonce;
  endif
  try
    add_property(o, prop, nonce, info);
  except e (E_INVARG)
    "A descendant defines the property: move the definition up here and put every stamp back.";
    stamps = this:tmoo_generation("stamps", o);
    if (!stamps)
      raise(e[1], e[2]);
    endif
    for entry in (stamps)
      if (prop in properties(entry[1]))
        delete_property(entry[1], prop);
      endif
    endfor
    add_property(o, prop, nonce, info);
    for entry in (stamps)
      entry[1].(prop) = entry[2];
      set_property_info(entry[1], prop, info);
    endfor
  endtry
  return nonce;
elseif (mode == "chparent")
  p = args[3];
  own = this:tmoo_generation("read", o);
  managed = this:tmoo_generation("managed", o);
  stamps = {};
  if (valid(p) && `property_info(p, prop) ! E_PROPNF => 0')
    "The new ancestry defines the property, so nothing in the subtree may.";
    stamps = this:tmoo_generation("stamps", o);
    if (prop in properties(o))
      delete_property(o, prop);
    else
      for entry in (stamps)
        if (prop in properties(entry[1]))
          delete_property(entry[1], prop);
        endif
      endfor
    endif
  endif
  try
    chparent(o, p);
  except e (ANY)
    if (own)
      this:tmoo_generation("stamp", o, own);
    endif
    for entry in (stamps)
      this:tmoo_generation("stamp", entry[1], entry[2]);
    endfor
    raise(e[1], e[2]);
  endtry
  "Leaving a defining ancestry drops the property from the whole subtree: restore the object first, then the rest.";
  if (own)
    this:tmoo_generation("stamp", o, own);
  endif
  for entry in (stamps)
    this:tmoo_generation("stamp", entry[1], entry[2]);
  endfor
  this:tmoo_generation("restamp", managed);
  return 1;
elseif (mode == "managed")
  out = {};
  reg = this.registry;
  for i in [1..length(reg[1])]
    d = reg[2][i];
    nonce = length(reg) > 2 ? reg[3][i] | "";
    if (nonce && valid(d) && d != o)
      a = parent(d);
      while (valid(a) && a != o)
        a = parent(a);
      endwhile
      if (a == o)
        out = {@out, {d, nonce}};
      endif
    endif
  endfor
  return out;
elseif (mode == "restamp")
  for entry in (o)
    if (valid(entry[1]) && this:tmoo_generation("read", entry[1]) != entry[2])
      this:tmoo_generation("stamp", entry[1], entry[2]);
    endif
  endfor
  return 1;
else
  raise(E_INVARG, tostr("unknown mode ", toliteral(mode)));
endif
