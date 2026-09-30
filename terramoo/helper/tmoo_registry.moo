":tmoo_registry(\"bootstrap\" | \"reconcile\" [, before]) maintains protected registry state.";
"It never suspends, so bootstrap's read/migration/write sequence is one atomic MOO task.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
mode = args[1];
props = properties(this);
reg = this.registry;
reg_length = length(reg);
registry_revision = reg_length > 3 ? reg[4] | 0;
if (reg_length == 2)
  nonces = {};
  for ignored in (reg[1])
    nonces = {@nonces, ""};
  endfor
  reg = {reg[1], reg[2], nonces, registry_revision};
elseif (reg_length == 3)
  reg = {reg[1], reg[2], reg[3], registry_revision};
endif
if (mode == "bootstrap")
  protected_revision = registry_revision;
  if ("_terramoo_registry_revision" in props && this._terramoo_registry_revision > protected_revision)
    protected_revision = this._terramoo_registry_revision;
  endif
  if ("_terramoo_registry_state" in props)
    saved = this._terramoo_registry_state;
    saved_revision = length(saved) > 3 ? saved[4] | 0;
    if (saved_revision > protected_revision)
      protected_revision = saved_revision;
    endif
  endif
  revision = registry_revision < protected_revision ? protected_revision + 1 | registry_revision;
  reg[4] = revision;
  if ("_terramoo_registry_revision" in props)
    this._terramoo_registry_revision = revision;
  else
    add_property(this, "_terramoo_registry_revision", revision, {this.owner, "r"});
  endif
  if ("_terramoo_registry_state" in props)
    this._terramoo_registry_state = reg;
  else
    add_property(this, "_terramoo_registry_state", reg, {this.owner, "r"});
  endif
  this.registry = reg;
  return reg;
elseif (mode == "reconcile")
  before = args[2];
  if (!("_terramoo_registry_revision" in props) || !("_terramoo_registry_state" in props))
    revision = registry_revision > before[4] ? registry_revision | before[4];
    reg[4] = revision;
    this.registry = reg;
    return reg;
  endif
  protected_revision = this._terramoo_registry_revision;
  current = this._terramoo_registry_state;
  revision = registry_revision > protected_revision ? registry_revision | protected_revision;
  if (before[4] > revision)
    revision = before[4];
  endif
  if (current[4] > revision)
    revision = current[4];
  endif
  payload_changed = !(equal(reg[1], before[1]) && equal(reg[2], before[2]) && equal(reg[3], before[3]));
  if (!payload_changed)
    if (current[4] > before[4])
      reg = current;
    elseif (registry_revision < protected_revision)
      revision = revision + 1;
    endif
  elseif (equal(reg[1], current[1]) && equal(reg[2], current[2]) && equal(reg[3], current[3]))
    "The callback's own nested tmoo_apply wrote this state; accept it.";
    reg = current;
  elseif (protected_revision > before[4])
    callback = reg;
    this._terramoo_registry_revision = current[4];
    this._terramoo_registry_state = current;
    this.registry = current;
    detail = "unknown keys";
    valid_shape = typeof(callback[1]) == typeof({}) && typeof(callback[2]) == typeof({}) && typeof(callback[3]) == typeof({}) && typeof(before[1]) == typeof({}) && typeof(before[2]) == typeof({}) && typeof(before[3]) == typeof({}) && length(callback[1]) == length(callback[2]) && length(callback[1]) == length(callback[3]) && length(before[1]) == length(before[2]) && length(before[1]) == length(before[3]);
    if (valid_shape)
      if (length(callback[1]) > 50 || length(before[1]) > 50)
        detail = "many keys";
      else
        touched = {};
        i = 0;
        for old_key in (before[1])
          i = i + 1;
          found = 0;
          j = 0;
          for new_key in (callback[1])
            j = j + 1;
            if (equal(old_key, new_key))
              found = j;
            endif
          endfor
          if (!found || !equal(before[2][i], callback[2][found]) || !equal(before[3][i], callback[3][found]))
            touched = setadd(touched, old_key);
          endif
        endfor
        j = 0;
        for new_key in (callback[1])
          j = j + 1;
          found = 0;
          i = 0;
          for old_key in (before[1])
            i = i + 1;
            if (equal(new_key, old_key))
              found = i;
            endif
          endfor
          if (!found || !equal(callback[2][j], before[2][found]) || !equal(callback[3][j], before[3][found]))
            touched = setadd(touched, new_key);
          endif
        endfor
        if (!touched)
          touched = callback[1];
        endif
        detail = toliteral(touched);
      endif
    endif
    raise(E_INVARG, tostr("registry conflict during callback; callback touched ", detail, "; rerun the operation"));
  else
    revision = revision + 1;
  endif
  reg[4] = revision;
  this._terramoo_registry_revision = revision;
  this._terramoo_registry_state = reg;
  this.registry = reg;
  return reg;
else
  raise(E_INVARG, tostr("unknown registry maintenance mode ", mode));
endif
