":tmoo_apply(ops [, may_suspend]) => one result per op: {1, value} or {0, \"E_NAME\", message}.";
"Every mutation carries {key, expected object, generation}; the helper verifies it immediately before changing the object.";
"The registry is {keys, objects, generations, revision}: three parallel lists and a CAS revision";
"(no maps: this runs on LambdaMOO 1.8),";
"and is written back after every change so a crash mid-apply leaves the MOO knowing what it made.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
ops = args[1];
may_suspend = length(args) > 1 && args[2];
out = {};
revision = 0;
protected_registry = "_terramoo_registry_state" in properties(this) && "_terramoo_registry_revision" in properties(this);
for op in (ops)
  try
    "Earlier ops or suspended tasks may have changed the registry.";
    reg = this.registry;
    reg_length = length(reg);
    registry_revision = reg_length > 3 ? reg[4] | 0;
    if (protected_registry)
      protected_revision = this._terramoo_registry_revision;
      revision = registry_revision;
      if (revision < protected_revision)
        revision = protected_revision + 1;
      endif
    elseif (registry_revision > revision)
      revision = registry_revision;
    endif
    if (reg_length == 2)
      nonces = {};
      for ignored in (reg[1])
        nonces = {@nonces, ""};
      endfor
      reg = {reg[1], reg[2], nonces, revision};
    elseif (reg_length == 3)
      reg = {reg[1], reg[2], reg[3], revision};
    else
      reg[4] = revision;
    endif
    if (protected_registry)
      this._terramoo_registry_revision = revision;
      this._terramoo_registry_state = reg;
    endif
    if (reg_length < 4 || registry_revision != revision)
      this.registry = reg;
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
      before = reg;
      r = create(p);
      "create() may run an :initialize verb that changes the registry.";
      reg = this.registry;
      reg_length = length(reg);
      registry_revision = reg_length > 3 ? reg[4] | 0;
      if (protected_registry)
        protected_revision = this._terramoo_registry_revision;
        current = this._terramoo_registry_state;
      endif
      if (reg_length == 2)
        nonces = {};
        for ignored in (reg[1])
          nonces = {@nonces, ""};
        endfor
        reg = {reg[1], reg[2], nonces, registry_revision};
      elseif (reg_length == 3)
        reg = {reg[1], reg[2], reg[3], registry_revision};
      endif
      if (protected_registry)
        revision = registry_revision > protected_revision ? registry_revision | protected_revision;
        if (current[4] > before[4] && equal(reg[1], before[1]) && equal(reg[2], before[2]) && equal(reg[3], before[3]))
          reg = current;
        elseif (registry_revision < protected_revision)
          revision = protected_revision + 1;
        endif
        reg[4] = revision;
        this._terramoo_registry_revision = revision;
        this._terramoo_registry_state = reg;
        this.registry = reg;
      else
        if (registry_revision > revision)
          revision = registry_revision;
        endif
        reg[4] = revision;
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
        reg = {{@reg[1], key}, {@reg[2], r}, {@reg[3], op[7]}, reg[4]};
      endif
      revision = protected_registry ? this._terramoo_registry_revision + 1 | reg[4] + 1;
      if (protected_registry)
        this._terramoo_registry_revision = revision;
      endif
      reg[4] = revision;
      if (protected_registry)
        this._terramoo_registry_state = reg;
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
        reg = {{@reg[1], key}, {@reg[2], r}, {@reg[3], nonce}, reg[4]};
      endif
      revision = protected_registry ? this._terramoo_registry_revision + 1 | reg[4] + 1;
      if (protected_registry)
        this._terramoo_registry_revision = revision;
      endif
      reg[4] = revision;
      if (protected_registry)
        this._terramoo_registry_state = reg;
      endif
      this.registry = reg;
    elseif (kind == "rename")
      i = op[2];
      expected_revision = op[3];
      o = op[4];
      nonce = op[5];
      new_key = op[6];
      if (typeof(i) != typeof(0) || i < 1 || i > length(reg[1]) || reg[4] != expected_revision || reg[2][i] != o || reg[3][i] != nonce)
        raise(E_INVARG, "registry binding changed before rename");
      endif
      old_key = reg[1][i];
      if (nonce && valid(o) && (!("_terramoo_generation" in properties(o)) || o.("_terramoo_generation") != nonce))
        raise(E_INVARG, tostr("key ", old_key, " names a different object generation"));
      endif
      j = new_key in reg[1];
      if (j && j != i)
        raise(E_INVARG, tostr("key ", reg[1][j], " is already registered as ", reg[2][j]));
      endif
      reg[1][i] = new_key;
      revision = protected_registry ? this._terramoo_registry_revision + 1 | reg[4] + 1;
      if (protected_registry)
        this._terramoo_registry_revision = revision;
      endif
      reg[4] = revision;
      if (protected_registry)
        this._terramoo_registry_state = reg;
      endif
      this.registry = reg;
      r = o;
    elseif (kind == "destroy")
      key = op[2];
      o = op[3];
      nonce = op[4];
      before = reg;
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
        reg_length = length(reg);
        registry_revision = reg_length > 3 ? reg[4] | 0;
        if (protected_registry)
          protected_revision = this._terramoo_registry_revision;
          current = this._terramoo_registry_state;
        endif
        if (reg_length == 2)
          nonces = {};
          for ignored in (reg[1])
            nonces = {@nonces, ""};
          endfor
          reg = {reg[1], reg[2], nonces, registry_revision};
        elseif (reg_length == 3)
          reg = {reg[1], reg[2], reg[3], registry_revision};
        endif
        if (protected_registry)
          revision = registry_revision > protected_revision ? registry_revision | protected_revision;
          if (current[4] > before[4] && equal(reg[1], before[1]) && equal(reg[2], before[2]) && equal(reg[3], before[3]))
            reg = current;
          elseif (registry_revision < protected_revision)
            revision = protected_revision + 1;
          endif
          reg[4] = revision;
          this._terramoo_registry_revision = revision;
          this._terramoo_registry_state = reg;
          this.registry = reg;
        else
          if (registry_revision > revision)
            revision = registry_revision;
          endif
          reg[4] = revision;
        endif
        i = key in reg[1];
      endif
      if (i)
        if (reg[2][i] != o || reg[3][i] != nonce)
          raise(E_INVARG, tostr("key ", reg[1][i], " changed during recycle"));
        endif
        revision = protected_registry ? this._terramoo_registry_revision + 1 | reg[4] + 1;
        if (protected_registry)
          this._terramoo_registry_revision = revision;
        endif
        reg = {listdelete(reg[1], i), listdelete(reg[2], i), listdelete(reg[3], i), revision};
        if (protected_registry)
          this._terramoo_registry_state = reg;
        endif
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
            src:add_exit(o);
            if (!(o in `src.exits ! ANY => {}'))
              raise(E_INVARG, tostr("exit ", o, " was not added to ", src, ".exits"));
            endif
            linked = {@linked, {o, "exit", src}};
          endif
          reg = this.registry;
          i = key in reg[1];
          if (!i || reg[2][i] != o || reg[3][i] != nonce || !valid(o) || !("_terramoo_generation" in properties(o)) || o.("_terramoo_generation") != nonce)
            raise(E_INVARG, tostr("key ", key, " changed during link"));
          endif
          if (valid(dst) && !(o in `dst.entrances ! ANY => {}'))
            dst:add_entrance(o);
            if (!(o in `dst.entrances ! ANY => {}'))
              raise(E_INVARG, tostr("exit ", o, " was not added to ", dst, ".entrances"));
            endif
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
      elseif (kind == "endpoint")
        prop = op[5];
        expected = op[6];
        new = op[7];
        if (prop != "source" && prop != "dest")
          raise(E_INVARG, "endpoint needs source or dest");
        endif
        current = o.(prop);
        if (current != expected)
          raise(E_INVARG, tostr(prop, " changed before endpoint update; replan"));
        endif
        if (length(op) >= 9)
          was_exit = op[8];
          will_exit = op[9];
        else
          exit_class = `$exit ! ANY => #-1';
          was_exit = 0;
          a = valid(o) && valid(exit_class) ? o | #-1;
          while (valid(a) && !was_exit)
            was_exit = a == exit_class;
            a = parent(a);
          endwhile
          will_exit = was_exit;
        endif
        if (!was_exit && !will_exit)
          "An ordinary source/dest property is only a compare-and-set update.";
          o.(prop) = new;
        else
          new_room = typeof(new) == typeof(#0) ? new | #-1;
          old_room = typeof(expected) == typeof(#0) ? expected | #-1;
          added_new = 0;
          adding_new = 0;
          changed_prop = 0;
          removed_old = 0;
          removing_old = 0;
          try
            "Callbacks may require the endpoint to name their room.";
            o.(prop) = new;
            changed_prop = 1;
            if (will_exit && valid(new_room) && (!was_exit || new_room != old_room))
              if (prop == "source" && !(o in `new_room.exits ! ANY => {}'))
                adding_new = 1;
                callback_result = new_room:add_exit(o);
                added_new = o in `new_room.exits ! ANY => {}';
                if (!callback_result || !added_new)
                  raise(E_INVARG, tostr("exit ", o, " was not added to ", new_room, ".exits"));
                endif
              elseif (prop == "dest" && !(o in `new_room.entrances ! ANY => {}'))
                adding_new = 1;
                callback_result = new_room:add_entrance(o);
                added_new = o in `new_room.entrances ! ANY => {}';
                if (!callback_result || !added_new)
                  raise(E_INVARG, tostr("exit ", o, " was not added to ", new_room, ".entrances"));
                endif
              endif
              "The callback may have suspended: recheck identity and the CAS value.";
              reg = this.registry;
              i = key in reg[1];
              if (!i || reg[2][i] != o || reg[3][i] != nonce || !valid(o) || !("_terramoo_generation" in properties(o)) || o.("_terramoo_generation") != nonce)
                raise(E_INVARG, tostr("key ", key, " changed during endpoint update"));
              endif
              if (o.(prop) != new)
                raise(E_INVARG, tostr(prop, " changed during endpoint update; replan"));
              endif
            endif
            if (was_exit && valid(old_room) && (!will_exit || old_room != new_room))
              if (prop == "source" && o in `old_room.exits ! ANY => {}')
                removing_old = 1;
                callback_result = old_room:remove_exit(o);
                removed_old = !(o in `old_room.exits ! ANY => {}');
                if (!callback_result || !removed_old)
                  raise(E_INVARG, tostr("exit ", o, " remains in ", old_room, ".exits"));
                endif
              elseif (prop == "dest" && o in `old_room.entrances ! ANY => {}')
                removing_old = 1;
                callback_result = old_room:remove_entrance(o);
                removed_old = !(o in `old_room.entrances ! ANY => {}');
                if (!callback_result || !removed_old)
                  raise(E_INVARG, tostr("exit ", o, " remains in ", old_room, ".entrances"));
                endif
              endif
              "The callback may have suspended: recheck identity and the new value.";
              reg = this.registry;
              i = key in reg[1];
              if (!i || reg[2][i] != o || reg[3][i] != nonce || !valid(o) || !("_terramoo_generation" in properties(o)) || o.("_terramoo_generation") != nonce)
                raise(E_INVARG, tostr("key ", key, " changed during endpoint update"));
              endif
              if (o.(prop) != new)
                raise(E_INVARG, tostr(prop, " changed during endpoint update; replan"));
              endif
            endif
          except endpoint_error (ANY)
            rollback = "";
            if (adding_new && !added_new)
              added_new = prop == "source" ? o in `new_room.exits ! ANY => {}' | o in `new_room.entrances ! ANY => {}';
            endif
            if (removing_old && !removed_old)
              removed_old = prop == "source" ? !(o in `old_room.exits ! ANY => {}') | !(o in `old_room.entrances ! ANY => {}');
            endif
            restored_prop = !changed_prop;
            if (changed_prop)
              current = `o.(prop) ! ANY => E_NONE';
              if (current == expected)
                restored_prop = 1;
              elseif (current == new)
                try
                  o.(prop) = expected;
                  restored_prop = 1;
                except rollback_error (ANY)
                  rollback = tostr(rollback, " could not restore ", prop, ";");
                endtry
              else
                rollback = tostr(rollback, " ", prop, " changed again before rollback;");
              endif
            endif
            restored_old = !removed_old;
            if (restored_prop && removed_old)
              if (prop == "source")
                callback_result = `old_room:add_exit(o) ! ANY => 0';
                if (!callback_result || !(o in `old_room.exits ! ANY => {}'))
                  rollback = tostr(rollback, " could not restore old exits membership;");
                  restored_old = 0;
                else
                  restored_old = 1;
                endif
              else
                callback_result = `old_room:add_entrance(o) ! ANY => 0';
                if (!callback_result || !(o in `old_room.entrances ! ANY => {}'))
                  rollback = tostr(rollback, " could not restore old entrances membership;");
                  restored_old = 0;
                else
                  restored_old = 1;
                endif
              endif
            endif
            if (restored_prop && !restored_old)
              rolled_forward = 0;
              try
                o.(prop) = new;
                rolled_forward = 1;
              except rollback_error (ANY)
                rollback = tostr(rollback, " could not return to new ", prop, ";");
              endtry
              if (rolled_forward && will_exit && valid(new_room))
                if (prop == "source" && !(o in `new_room.exits ! ANY => {}'))
                  callback_result = `new_room:add_exit(o) ! ANY => 0';
                  added_new = o in `new_room.exits ! ANY => {}';
                elseif (prop == "dest" && !(o in `new_room.entrances ! ANY => {}'))
                  callback_result = `new_room:add_entrance(o) ! ANY => 0';
                  added_new = o in `new_room.entrances ! ANY => {}';
                endif
                if (!added_new)
                  rollback = tostr(rollback, " could not restore new room membership;");
                endif
              endif
            endif
            if (restored_prop && restored_old && added_new)
              if (prop == "source")
                callback_result = `new_room:remove_exit(o) ! ANY => 0';
                if (!callback_result || o in `new_room.exits ! ANY => {}')
                  rollback = tostr(rollback, " could not remove new exits membership;");
                endif
              else
                callback_result = `new_room:remove_entrance(o) ! ANY => 0';
                if (!callback_result || o in `new_room.entrances ! ANY => {}')
                  rollback = tostr(rollback, " could not remove new entrances membership;");
                endif
              endif
            endif
            if (rollback)
              raise(E_INVARG, tostr(endpoint_error[2], "; rollback failed:", rollback));
            endif
            raise(endpoint_error[1], endpoint_error[2]);
          endtry
        endif
      elseif (kind == "clearprop")
        clear_property(o, op[5]);
      elseif (kind == "unlink")
        relation = op[5];
        room = typeof(op[6]) == typeof(#0) ? op[6] | #-1;
        if (relation != "exit" && relation != "entrance")
          raise(E_INVARG, tostr("unknown unlink relation ", relation));
        endif
        exit_class = `$exit ! ANY => #-1';
        is_exit = 0;
        a = valid(o) && valid(exit_class) ? o | #-1;
        while (valid(a) && !is_exit)
          is_exit = a == exit_class;
          a = parent(a);
        endwhile
        if (is_exit && valid(room) && relation == "exit" && o in `room.exits ! ANY => {}')
          room:remove_exit(o);
          if (o in `room.exits ! ANY => {}')
            raise(E_INVARG, tostr("exit ", o, " remains in ", room, ".exits"));
          endif
        elseif (is_exit && valid(room) && relation == "entrance" && o in `room.entrances ! ANY => {}')
          room:remove_entrance(o);
          if (o in `room.entrances ! ANY => {}')
            raise(E_INVARG, tostr("exit ", o, " remains in ", room, ".entrances"));
          endif
        endif
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
