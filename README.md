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

## Experiments

```sh
just experiments                 # stored experiments, with titles
just reproduce conns-plateau     # env check, build, sweep, plot
just plot smoltcp-s3             # replot from an existing CSV
```

An experiment is a TOML file in `experiments/` holding every parameter that
decides what the numbers mean — instance, axis, held-constant knobs, reps,
cooldowns, the VM cap — so reproducing one needs nothing but its name. Add
`--dry-run` to print the plan without launching anything.

Results land in `results/<bench>/`: a CSV of one row per run, plus a `.png` and
a `.md` table per plot. Sweeps resume from the CSV, so an interrupted run
continues where it stopped.

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
