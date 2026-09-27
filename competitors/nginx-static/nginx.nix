# A static nginx for the bench instances, which have no package mirror: only
# what a file server needs, and nixpkgs' generic --enable-static stripped,
# since nginx's configure rejects it (its build is static by the linker flags).
let
  pkgs = import <nixpkgs> { };
  ps = pkgs.pkgsStatic;
  lib = pkgs.lib;
in
(ps.nginx.override { modules = [ ]; withStream = false; withMail = false; withKTLS = false; })
.overrideAttrs (o: {
  dontAddStaticConfigureFlags = true;
  dontDisableStatic = true;
  configureFlags = lib.subtractLists [ "--enable-static" "--disable-shared" ] (o.configureFlags or [ ]);
  preConfigure = (o.preConfigure or "") + ''
    configureFlagsArray=("''${configureFlagsArray[@]/--enable-static}")
    configureFlagsArray=("''${configureFlagsArray[@]/--disable-shared}")
  '';
})
