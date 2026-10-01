# A static wrk for the bench instances, which have no package mirror. Two
# things nixpkgs' static build gets wrong: the build-time luajit that compiles
# wrk.lua to bytecode is missing (the native one supplies it; the bytecode is
# identical), and LuaJIT finds that bytecode at run time through
# dlsym(RTLD_DEFAULT, "luaJIT_BC_wrk"), which a static musl binary cannot do --
# `require "wrk"` then yields nil and wrk panics before reading any script.
# The patch loads the symbol directly, with the unbounded length LuaJIT's own
# loader passes (a bytecode dump is self-delimiting).
let pkgs = import <nixpkgs> { }; in
pkgs.pkgsStatic.wrk.overrideAttrs (o: {
  nativeBuildInputs = (o.nativeBuildInputs or [ ]) ++ [ pkgs.luajit ];
  postPatch = (o.postPatch or "") + ''
    substituteInPlace src/script.c --replace-fail \
      '(void) luaL_dostring(L, "wrk = require \"wrk\"");' \
      'extern const unsigned char luaJIT_BC_wrk[];
    if (luaL_loadbuffer(L, (const char *) luaJIT_BC_wrk, ~(size_t) 0, "wrk") || lua_pcall(L, 0, 1, 0)) {
        fprintf(stderr, "wrk.lua: %s\n", lua_tostring(L, -1));
        exit(1);
    }
    lua_setglobal(L, "wrk");'
  '';
})
