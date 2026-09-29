":tmoo_apply(ops [, may_suspend]) => one result per op: {1, value} or {0, \"E_NAME\", message}.";
"Every mutation carries {key, expected object, generation}; the helper verifies it immediately before changing the object.";
"The registry is {keys, objects, generations}, three parallel lists (no maps: this runs on LambdaMOO 1.8),";
"and is written back after every change so a crash mid-apply leaves the MOO knowing what it made.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
ops = args[1];
may_suspend = length(args) > 1 && args[2];
out = {};
for op in (ops)
  try
    "Earlier ops or suspended tasks may have changed the registry.";
    reg = this.registry;
    if (length(reg) == 2)
      nonces = {};
      for ignored in (reg[1])
        nonces = {@nonces, ""};
      endfor
      reg = {reg[1], reg[2], nonces};
    endif
    kind = op[1];
    r = 1;
    if (kind == "create")
      key = op[2];
      expected = op[3];
      expected_nonce = op[4];
      i = key in reg[1];
      if (expected == #-1)
        if (i)
          raise(E_INVARG, tostr("key ", reg[1][i], " is now registered as ", reg[2][i]));
        endif
      else
        if (!i || reg[2][i] != expected || reg[3][i] != expected_nonce)
          raise(E_INVARG, tostr("key ", key, " changed before create"));
        endif
        if (valid(expected))
          if (!expected_nonce || !("_terramoo_generation" in properties(expected)) || expected.("_terramoo_generation") != expected_nonce)
            raise(E_INVARG, tostr("key ", key, " names a different object generation"));
          endif
          raise(E_INVARG, tostr("key ", key, " is no longer gone"));
        endif
      endif
      "op[5] is the parent: an object, or the registry key of one created earlier in this batch.";
      p = op[5];
      if (typeof(p) == typeof(""))
        i = p in reg[1];
        if (!i)
          raise(E_INVARG, tostr("no parent ", p, " in the registry"));
        endif
        p = reg[2][i];
      endif
      r = create(p);
      "create() may run an :initialize verb that changes the registry.";
      reg = this.registry;
      if (length(reg) == 2)
        nonces = {};
        for ignored in (reg[1])
          nonces = {@nonces, ""};
        endfor
        reg = {reg[1], reg[2], nonces};
      endif
      j = r in reg[2];
      if (j && reg[1][j] != key)
        raise(E_INVARG, tostr("object ", r, " is already registered as ", reg[1][j], "; cannot bind key ", key));
      endif
      i = key in reg[1];
      if (expected == #-1 ? i | (!i || reg[2][i] != expected || reg[3][i] != expected_nonce))
        raise(E_INVARG, tostr("key ", key, " changed during create"));
      endif
      r.name = op[6];
      add_property(r, "_terramoo_generation", op[7], {this.owner, "r"});
      if (i)
        reg[2][i] = r;
        reg[3][i] = op[7];
      else
        reg = {{@reg[1], key}, {@reg[2], r}, {@reg[3], op[7]}};
      endif
      this.registry = reg;
    elseif (kind == "register")
      key = op[2];
      r = op[3];
      nonce = op[4];
      i = key in reg[1];
      if (i && reg[2][i] != r)
        raise(E_INVARG, tostr("key ", reg[1][i], " is already registered as ", reg[2][i]));
      endif
      j = r in reg[2];
      if (j && j != i)
        raise(E_INVARG, tostr("object ", r, " is already registered as ", reg[1][j]));
      endif
      if ("_terramoo_generation" in properties(r))
        r.("_terramoo_generation") = nonce;
        set_property_info(r, "_terramoo_generation", {this.owner, "r"});
      else
        add_property(r, "_terramoo_generation", nonce, {this.owner, "r"});
      endif
      if (i)
        reg[2][i] = r;
        reg[3][i] = nonce;
      else
        reg = {{@reg[1], key}, {@reg[2], r}, {@reg[3], nonce}};
      endif
      this.registry = reg;
    elseif (kind == "destroy")
      key = op[2];
      o = op[3];
      nonce = op[4];
      i = key in reg[1];
      if (i)
        if (reg[2][i] != o || reg[3][i] != nonce)
          raise(E_INVARG, tostr("key ", key, " changed before destroy"));
        endif
        if (!nonce)
          raise(E_INVARG, tostr("key ", key, " has an unverified legacy binding"));
        endif
        if (valid(o))
          if (!("_terramoo_generation" in properties(o)) || o.("_terramoo_generation") != nonce)
            raise(E_INVARG, tostr("key ", key, " names a different object generation"));
          endif
          recycle(o);
        endif
        if (valid(o))
          raise(E_INVARG, tostr("object ", o, " is still valid after recycle"));
        endif
        ":recycle may register objects, remove keys or replace this binding.";
        reg = this.registry;
        if (length(reg) == 2)
          nonces = {};
          for ignored in (reg[1])
            nonces = {@nonces, ""};
          endfor
          reg = {reg[1], reg[2], nonces};
        endif
        i = key in reg[1];
      endif
      if (i)
        if (reg[2][i] != o || reg[3][i] != nonce)
          raise(E_INVARG, tostr("key ", reg[1][i], " changed during recycle"));
        endif
        reg = {listdelete(reg[1], i), listdelete(reg[2], i), listdelete(reg[3], i)};
        this.registry = reg;
      endif
    elseif (kind == "link")
      "Attach every verified exit to its source and destination rooms, LambdaCore style.";
      exit_class = `$exit ! ANY => #-1';
      linked = {};
      for binding in (op[2])
        key = binding[1];
        o = binding[2];
        nonce = binding[3];
        reg = this.registry;
        i = key in reg[1];
        if (!i || reg[2][i] != o || reg[3][i] != nonce || !nonce || !valid(o) || !("_terramoo_generation" in properties(o)) || o.("_terramoo_generation") != nonce)
          raise(E_INVARG, tostr("key ", key, " changed before link"));
        endif
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
          reg = this.registry;
          i = key in reg[1];
          if (!i || reg[2][i] != o || reg[3][i] != nonce || !valid(o) || !("_terramoo_generation" in properties(o)) || o.("_terramoo_generation") != nonce)
            raise(E_INVARG, tostr("key ", key, " changed during link"));
          endif
          if (valid(dst) && !(o in `dst.entrances ! ANY => {}'))
            `dst:add_entrance(o) ! ANY => 0';
            linked = {@linked, {o, "entrance", dst}};
          endif
        endif
      endfor
      r = linked;
    else
      key = op[2];
      o = op[3];
      nonce = op[4];
      i = key in reg[1];
      if (!i || reg[2][i] != o || reg[3][i] != nonce || !nonce || !valid(o) || !("_terramoo_generation" in properties(o)) || o.("_terramoo_generation") != nonce)
        raise(E_INVARG, tostr("key ", key, " changed before ", kind));
      endif
      if (kind == "name")
        o.name = op[5];
      elseif (kind == "chparent")
        chparent(o, op[5]);
      elseif (kind == "move")
        move(o, op[5]);
      elseif (kind == "flags")
        f = op[5];
        o.r = index(f, "r") > 0;
        o.w = index(f, "w") > 0;
        o.f = index(f, "f") > 0;
      elseif (kind == "addprop")
        add_property(o, op[5], op[6], op[7]);
      elseif (kind == "rmprop")
        delete_property(o, op[5]);
      elseif (kind == "propinfo")
        set_property_info(o, op[5], op[6]);
      elseif (kind == "setprop")
        o.(op[5]) = op[6];
      elseif (kind == "clearprop")
        clear_property(o, op[5]);
      elseif (kind == "addverb" || kind == "verbcode")
        if (kind == "addverb")
          add_verb(o, op[5], op[6]);
          errs = set_verb_code(o, length(verbs(o)), op[7]);
        else
          errs = set_verb_code(o, op[5], op[6]);
        endif
        if (errs)
          msg = "";
          for e in (errs)
            msg = msg ? tostr(msg, " / ", e) | tostr(e);
          endfor
          raise(E_INVARG, tostr("compile error: ", msg));
        endif
      elseif (kind == "rmverb")
        delete_verb(o, op[5]);
      elseif (kind == "verbinfo")
        set_verb_info(o, op[5], op[6]);
      elseif (kind == "verbargs")
        set_verb_args(o, op[5], op[6]);
      else
        raise(E_INVARG, tostr("unknown op ", kind));
      endif
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
