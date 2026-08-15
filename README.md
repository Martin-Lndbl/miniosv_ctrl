# miniosv_ctrl
MiniOSv Control Center - Coordination of benchmarks, applications and plots

## Getting Started

```sh
just --list                          # available recipes
just setup apps/bench/smoltcp-s3     # probe the environment, write .env
just build apps/bench/smoltcp-s3 -j8 # extra args are passed to make
just clean apps/bench/smoltcp-s3     # delete the bucket and .env again
```

`setup`, `build` and `clean` all take a path to the app. `setup` and `clean`
forward to the recipe of the same name in `<app>/justfile`, which can also be run
from that directory directly. `setup` logs into AWS if needed; `clean` prompts
before deleting, and a trailing `force` skips the prompt.

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
