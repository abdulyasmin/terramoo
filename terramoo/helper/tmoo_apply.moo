":tmoo_apply(ops [, may_suspend]) => one result per op: {1, value} or {0, \"E_NAME\", message}.";
"Every op is a list whose first element names it; see terramoo/apply.py for the table.";
"The registry is {keys, objects}, two parallel lists (no maps: this runs on LambdaMOO 1.8),";
"and is written back after every change so a crash mid-apply leaves the MOO knowing what it made.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
ops = args[1];
may_suspend = length(args) > 1 && args[2];
reg = this.registry;
out = {};
for op in (ops)
  try
    kind = op[1];
    if (kind == "create")
      "op[3] is the parent: an object, or the registry key of one created earlier in this batch.";
      p = op[3];
      if (typeof(p) == typeof(""))
        i = p in reg[1];
        p = i ? reg[2][i] | E_INVARG;
        if (typeof(p) == typeof(E_NONE))
          raise(E_INVARG, tostr("no parent ", op[3], " in the registry"));
        endif
      endif
      o = create(p);
      o.name = op[4];
      i = op[2] in reg[1];
      if (i)
        reg[2][i] = o;
      else
        reg = {{@reg[1], op[2]}, {@reg[2], o}};
      endif
      this.registry = reg;
      r = o;
    elseif (kind == "recycle")
      recycle(op[2]);
      r = 1;
    elseif (kind == "register")
      i = op[2] in reg[1];
      if (i)
        reg[2][i] = op[3];
      else
        reg = {{@reg[1], op[2]}, {@reg[2], op[3]}};
      endif
      this.registry = reg;
      r = op[3];
    elseif (kind == "unregister")
      i = op[2] in reg[1];
      if (i)
        reg = {listdelete(reg[1], i), listdelete(reg[2], i)};
        this.registry = reg;
      endif
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
    elseif (kind == "addverb" || kind == "verbcode")
      if (kind == "addverb")
        add_verb(op[2], op[3], op[4]);
        errs = set_verb_code(op[2], length(verbs(op[2])), op[5]);
      else
        errs = set_verb_code(op[2], op[3], op[4]);
      endif
      if (errs)
        msg = "";
        for e in (errs)
          msg = msg ? tostr(msg, " / ", e) | tostr(e);
        endfor
        raise(E_INVARG, tostr("compile error: ", msg));
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
    elseif (kind == "link")
      "Attach every exit among the objects to its source and destination rooms, LambdaCore style.";
      "A core with no $exit, or exits that work differently, simply links nothing.";
      exit_class = `$exit ! ANY => #-1';
      linked = {};
      for o in (op[2])
        is_exit = 0;
        a = valid(o) && valid(exit_class) ? o | #-1;
        while (valid(a) && !is_exit)
          is_exit = a == exit_class;
          a = parent(a);
        endwhile
        if (is_exit)
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
      raise(E_INVARG, tostr("unknown op ", kind));
    endif
    out = {@out, {1, r}};
  except e (ANY)
    out = {@out, {0, toliteral(e[1]), e[2]}};
  endtry
  if (may_suspend && (ticks_left() < 10000 || seconds_left() < 2))
    suspend(0);
  endif
endfor
return out;
