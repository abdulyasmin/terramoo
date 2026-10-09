"Player deployment revision and a durable record of the last field operation.";
if (caller_perms() != this.owner || player != this.owner || !is_player(this.owner))
  raise(E_PERM);
endif
present = "_terramoo_player_state" in properties(this);
state = present ? this._terramoo_player_state | {1, "", 0, "", 0, "idle", {}};
mode = args[1];
if (mode == "read")
  return state;
elseif (mode == "bindings")
  for binding in (args[2])
    reg = this.registry;
    i = binding[1] in reg[1];
    if (!i || reg[2][i] != binding[2] || reg[3][i] != binding[3] || !binding[3] || !valid(binding[2]) || this:tmoo_generation("read", binding[2]) != binding[3])
      raise(E_INVARG, "referenced object changed during player operation");
    endif
  endfor
  return 1;
elseif (mode == "begin")
  world = args[2];
  revision = args[3];
  token = args[4];
  reg = this.registry;
  packages = this:tmoo_packages("read");
  if (state[4] || state[3] != revision || state[2] && state[2] != world)
    raise(E_INVARG, "player deployment revision changed or recovery is required");
  endif
  if (reg[4] != args[5] || packages[3] != args[6])
    raise(E_INVARG, "object deployment changed; replan player changes");
  endif
  state = {1, world, revision + 1, token, 0, "ready", {}};
elseif (mode == "apply")
  token = args[2];
  step = args[3];
  if (!token || token != state[4] || step != state[5] + 1 || !(state[6] in {"ready", "done"}))
    raise(E_INVARG, "player operation cannot be replayed; recover first");
  endif
  this:tmoo_player("bindings", args[7]);
  this:tmoo_player_write("check", args[4], args[5], args[6]);
  state[5] = step;
  state[6] = "running";
  state[7] = {task_id()};
  this._terramoo_player_state = state;
  held = state;
  try
    result = this:tmoo_player_write("apply", args[4], args[5], args[6], token);
    this:tmoo_player("bindings", args[7]);
    if (!equal(this._terramoo_player_state, state))
      raise(E_INVARG, "player deployment changed during callback");
    endif
    state[6] = "done";
    state[7] = {1, result};
  except e (ANY)
    state[6] = "failed";
    state[7] = {0, toliteral(e[1]), e[2]};
  endtry
  if (!equal(this._terramoo_player_state, held))
    raise(E_INVARG, "player deployment changed during callback; result was not recorded");
  endif
elseif (mode == "finish")
  if (!args[2] || state[4] != args[2])
    raise(E_INVARG, "wrong player recovery token");
  endif
  if (state[6] in {"running", "failed"} && !(length(args) > 2 && args[3]))
    raise(E_INVARG, "player callback outcome requires explicit reconciliation");
  endif
  if (state[6] == "running")
    "queued_tasks omits callbacks suspended under another verb owner's permissions.";
    alive = 1;
    try
      "mooR implements valid_task, but its task_stack stub aborts even inside try.";
      alive = call_function("valid_task", state[7][1]);
    except (E_INVARG)
      "LambdaMOO lacks valid_task. Its task_stack distinguishes gone from private.";
      try
        task_stack(state[7][1]);
      except (E_INVARG)
        alive = 0;
      except (E_PERM)
        "The task exists, but its stack is private.";
      endtry
    endtry
    if (alive)
      raise(E_INVARG, "player callback is still running; wait for it to finish before recovery");
    endif
  endif
  state[4] = "";
  state[6] = "finished";
else
  raise(E_INVARG, "unknown player deployment operation");
endif
if (present)
  this._terramoo_player_state = state;
else
  add_property(this, "_terramoo_player_state", state, {this.owner, ""});
endif
return state;
