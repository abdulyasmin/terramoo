":tmoo_callback(object, verb, argument) runs a callback and always reconciles the registry.";
if (caller_perms() != this.owner && !caller_perms().wizard)
  raise(E_PERM);
endif
before = this.registry;
try
  result = args[1]:(args[2])(args[3]);
finally
  this:tmoo_registry("reconcile", before);
endtry
return result;
