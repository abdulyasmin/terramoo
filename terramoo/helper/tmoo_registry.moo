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
  if (current[4] > before[4] && equal(reg[1], before[1]) && equal(reg[2], before[2]) && equal(reg[3], before[3]))
    reg = current;
  elseif (registry_revision < protected_revision)
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
