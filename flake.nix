{
  description = "miniosv_ctrl — miniosv devshells extended with control-center tooling";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    flake-utils.url = "github:numtide/flake-utils";

    miniosv = {
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
        extraPackages = with pkgs; [
          just
          (python3.withPackages (
            ps: with ps; [
              # We need to redeclare every python
              # dependency from the miniosv shell
              awscrt
              boto3
              botocore
              pyyaml
            ]
          ))
        ];

        extend =
          shell:
          shell.overrideAttrs (old: {
            nativeBuildInputs = extraPackages ++ (old.nativeBuildInputs or [ ]);

            shellHook = (old.shellHook or "") + ''
              if [ -f "$PWD/.env" ]; then
                set -a
                . "$PWD/.env"
                set +a
              else
                echo "no .env — run 'just setup'" >&2
              fi
            '';
          });
      in
      {
        devShells = builtins.mapAttrs (_name: extend) miniosv.devShells.${system};
      }
    );
}
