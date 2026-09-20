":tmoo_apply(ops) => one result per op: {1, value} or {0, \"E_NAME\", message}.";
"Every op is a list whose first element names it; see terramoo/apply.py for the table.";
"The registry (name -> object) lives in this.registry and is updated here so a";
"crash mid-apply leaves the MOO knowing what it created.";
reg = this.registry;
out = {};
for op in (args[1])
  try
    kind = op[1];
    if (kind == "create")
      "op[3] is the parent: an object, or the registry name of one created earlier in this batch.";
      p = op[3];
      if (typeof(p) == STR)
        p = reg[p];
      endif
      o = create(p);
      o.name = op[4];
      reg[op[2]] = o;
      this.registry = reg;
      r = o;
    elseif (kind == "recycle")
      recycle(op[2]);
      r = 1;
    elseif (kind == "register")
      reg[op[2]] = op[3];
      this.registry = reg;
      r = op[3];
    elseif (kind == "unregister")
      reg = mapdelete(reg, op[2]);
      this.registry = reg;
      r = 1;
    elseif (kind == "name")
      op[2].name = op[3];
      r = 1;
    elseif (kind == "chparent")
      chparent(op[2], op[3]);
      r = 1;
    elseif (kind == "move")
      move(op[2], op[3]);
      r = 1;
    elseif (kind == "flags")
      o = op[2];
      f = op[3];
      o.r = index(f, "r") > 0;
      o.w = index(f, "w") > 0;
      o.f = index(f, "f") > 0;
      r = 1;
    elseif (kind == "addprop")
      add_property(op[2], op[3], op[4], op[5]);
      r = 1;
    elseif (kind == "rmprop")
      delete_property(op[2], op[3]);
      r = 1;
    elseif (kind == "propinfo")
      set_property_info(op[2], op[3], op[4]);
      r = 1;
    elseif (kind == "setprop")
      op[2].(op[3]) = op[4];
      r = 1;
    elseif (kind == "clearprop")
      clear_property(op[2], op[3]);
      r = 1;
    elseif (kind == "addverb")
      add_verb(op[2], op[3], op[4]);
      errs = set_verb_code(op[2], length(verbs(op[2])), op[5]);
      if (errs)
        out = {@out, {0, "E_COMPILE", $string_utils:from_list(errs, " / ")}};
        continue;
      endif
      r = 1;
    elseif (kind == "rmverb")
      delete_verb(op[2], op[3]);
      r = 1;
    elseif (kind == "verbinfo")
      set_verb_info(op[2], op[3], op[4]);
      r = 1;
    elseif (kind == "verbargs")
      set_verb_args(op[2], op[3], op[4]);
      r = 1;
    elseif (kind == "verbcode")
      errs = set_verb_code(op[2], op[3], op[4]);
      if (errs)
        out = {@out, {0, "E_COMPILE", $string_utils:from_list(errs, " / ")}};
        continue;
      endif
      r = 1;
    elseif (kind == "link")
      "Attach every exit among the objects to its source and destination rooms.";
      linked = {};
      for o in (op[2])
        if (valid(o) && isa(o, $exit))
          src = `o.source ! ANY => #-1';
          dst = `o.dest ! ANY => #-1';
          if (valid(src) && !(o in `src.exits ! ANY => {}'))
            `src:add_exit(o) ! ANY => 0';
            linked = {@linked, {o, "exit", src}};
          endif
          if (valid(dst) && !(o in `dst.entrances ! ANY => {}'))
            `dst:add_entrance(o) ! ANY => 0';
            linked = {@linked, {o, "entrance", dst}};
          endif
        endif
      endfor
      r = linked;
    else
      out = {@out, {0, "E_INVARG", tostr("unknown op ", kind)}};
      continue;
    endif
    out = {@out, {1, r}};
  except e (ANY)
    out = {@out, {0, tostr(e[1]), e[2]}};
  endtry
endfor
return out;
