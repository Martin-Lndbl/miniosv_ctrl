{
  description = "miniosv_ctrl — miniosv devshells extended with control-center tooling";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    flake-utils.url = "github:numtide/flake-utils";

    miniosv = {
      # The submodule is fetched as its own git repo rather than via
      # `path:./miniosv`, which would need `nix develop '.?submodules=1'`
      # because submodule contents are invisible in the parent's git tree.
      # Run `nix flake update miniosv` after moving the submodule.
      url = "git+file:./miniosv";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.flake-utils.follows = "flake-utils";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      miniosv,
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        # Packages layered on top of every miniosv devshell.  Everything the
        # child shell already provides (packages *and* env vars such as
        # OVMF_CODE) is inherited, so only list what is missing here.
        extraPackages = with pkgs; [
          # e.g. jq
          # e.g. (python3.withPackages (ps: [ ps.matplotlib ]))
        ];

        # Extend a child shell without dropping anything it already sets.
        # mkShell puts its `packages` into nativeBuildInputs, which is also what
        # miniosv's shells use, so appending there is enough.
        extend =
          shell:
          shell.overrideAttrs (old: {
            nativeBuildInputs = extraPackages ++ (old.nativeBuildInputs or [ ]);
          });
      in
      {
        # One output per devshell exposed by miniosv (default, aws, cli, …).
        devShells = builtins.mapAttrs (_name: extend) miniosv.devShells.${system};
      }
    );
}
