# miniosv_ctrl
MiniOSv Control Center - Coordination of benchmarks, applications and plots

## Getting Started

```sh
just --list            # available recipes
just setup smoltcp-s3  # probe the environment, write .env
just build <app> -j8   # extra args are passed to make
just clean smoltcp-s3  # delete the bucket and .env again
```

`setup` and `clean` are per bench: they forward to the recipe of the same name in
`apps/bench/<name>/justfile` (so the smoltcp-s3 pair lives in the `apps` submodule,
at `apps/bench/smoltcp-s3/justfile`, and can also be run from that directory as a
plain `just setup` / `just clean`).
`setup` runs `aws login --remote` first if you are not logged in; `clean` prompts
before deleting, and `just clean smoltcp-s3 force` skips the prompt.

## Devshells
```sh
nix develop            # extended miniosv default shell
nix develop .#cli      # extended miniosv cli shell
nix flake show         # list available shells
```

The submodule is consumed as a git repo (`git+file:./miniosv`), so run the
commands from the repository root, and refresh the pin after moving the
submodule to a different commit:

```sh
nix flake update miniosv
```
