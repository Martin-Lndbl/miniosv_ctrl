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
              pandas
              matplotlib
              tabulate
            ]
          ))
        ];

        extend =
          shell:
          shell.overrideAttrs (old: {
            nativeBuildInputs = extraPackages ++ (old.nativeBuildInputs or [ ]);

            shellHook = (old.shellHook or "") + ''
              # competitors/linux-s3 links statically, so it runs on a AL2023 AMI.
              export GLIBC_STATIC_LIB="${pkgs.glibc.static}/lib"

              if [ -f "$PWD/.env" ]; then
                set -a
                . "$PWD/.env"
                set +a
              else
                echo "no .env — run 'just setup'" >&2
              fi

              ctrl_root=$(git rev-parse --show-superproject-working-tree 2>/dev/null || true)
              [ -n "$ctrl_root" ] || ctrl_root=$(git rev-parse --show-toplevel 2>/dev/null || true)
              if [ -n "$ctrl_root" ] && [ -e "$ctrl_root/miniosv/.git" ]; then
                git -C "$ctrl_root/miniosv" config remote.upstream.url >/dev/null 2>&1 ||
                  git -C "$ctrl_root/miniosv" remote add upstream git@github.com:miniosv/miniosv.git
                git -C "$ctrl_root/miniosv" config remote.kite.url >/dev/null 2>&1 ||
                  git -C "$ctrl_root/miniosv" remote add kite git@github.com:TUM-DSE/miniosv.git
                git -C "$ctrl_root/miniosv" config remote.pushDefault origin
                git -C "$ctrl_root/miniosv" config checkout.defaultRemote origin
              fi
            '';
          });
      in
      {
        devShells = builtins.mapAttrs (_name: extend) miniosv.devShells.${system};
      }
    );
}
