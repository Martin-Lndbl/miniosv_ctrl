# miniosv_ctrl
MiniOSv Control Center — coordination of benchmarks, applications and plots

A benchmark here is not a script you run; it is a TOML file in `experiments/`
that says what is measured, on which machine, against which baseline, and why.
`just reproduce <name>` turns one back into numbers and a figure.

## Layout

```
miniosv/       the kernel, a submodule; `just build` makes a boot image from it
apps/          guest applications, a submodule; one directory per bench
               (and two submodules of its own: DuckDB and its httpfs extension)
competitors/   the Linux baselines the guest arms are subtracted from
experiments/   one TOML per question, grouped by subject (s3, tpch, pmc)
scripts/bench/ the sweep runner, the per-bench drivers, the plotter
results/       CSVs and figures, derived from the experiment's own path
```

Every bench — guest or competitor — carries its own `justfile` with `setup`
and `clean`, and the shell half they share lives in `scripts/bench-setup.sh`.
`apps/bench/smoltcp-s3` owns the bucket, the subnet and the blob; the other
benches reuse them, so setting one of those up bootstraps through it rather
than making a second set.

`apps/bench/duckdb-tpch` is the exception to "one directory of sources per
bench": DuckDB and the httpfs extension are whole upstream trees, so they are
submodules at `apps/miniduckdb` and `apps/miniduckdb-httpfs` and the bench
directory holds only the `Makefile` naming them. Nothing else needs them, so a
checkout that will not build that bench can leave them uninitialised.

## Getting started

```sh
just --list                              # available recipes
just setup apps/bench/smoltcp-s3         # probe the environment, write .env
just setup apps/bench/duckdb-tpch 1,0.1  # trailing args go to the bench's setup
just build apps/bench/smoltcp-s3         # boot image; extra args go to make
just clean apps/bench/smoltcp-s3         # delete the bucket and .env again
```

`setup`, `build` and `clean` all take a path to the bench, and the path may be
under `apps/` or under `competitors/`. `setup` and `clean` forward to the recipe
of the same name in `<bench>/justfile`, which can also be run from that
directory directly. `setup` logs into AWS if needed; `clean` prompts before
deleting, and a trailing `force` skips the prompt. `build` already passes `-j`,
so extra args are for make itself (`arch=aarch64`, …).

Booting and deploying by hand:

```sh
just run --arch aarch64 -m 4G  # QEMU; args go to miniosv/scripts/run.py
just deploy c6in.8xlarge       # EC2 in $AWS_SUBNET; args go to aws-deploy.py
```

## Experiments

```sh
just experiments                     # stored experiments, with titles
just reproduce s3/miniosv-tls-conns  # env check, build, sweep, plot
just reproduce miniosv-tls-conns     # bare names work while they stay unique
```

An experiment holds every parameter that decides what the numbers mean —
bench, instance, axis, held-constant knobs, compiled-in `[env]`, reps,
cooldowns, the VM cap, the pinned S3 front-end — plus prose saying what it
measures and what has already been ruled out. Reproducing one needs nothing
but its name.

They are grouped by subject rather than by the knob they sweep: an axis is a
property of a run, not an identity, and grouping by it once gave three
unrelated files the same name.

Three kinds, distinguished by which keys the file sets:

| key | what `reproduce` does |
| --- | --- |
| `axis` + `[[points]]` | runs the sweep driver once per point into one CSV |
| `deploy` | hands off to that bench's own `reproduce` recipe, which owns its boot loop |
| `prep` | reshapes already-captured results through `scripts/bench/pmc-prep.py` |

Each `[[points]]` entry is one configuration, run as its own sweep invocation
into a shared CSV — separate invocations because a knob other than the axis may
co-vary with the axis, which a single `--sweep` cannot express. The resume key
is `(axis, axis_value, rep)`, so an interrupted experiment continues where it
stopped.

Useful flags: `--dry-run` prints the plan without launching anything,
`--no-plot` stops after the data, and `--reps`, `--cooldown` and
`--point-cooldown` override the file for one run.

Experiments ask for spot instances, about a tenth of the on-demand price (a
c6in.16xlarge was $0.39 against $3.90 an hour in eu-north-1 on 2026-09-18),
and take on-demand when there is no spot capacity: `--market` defaults to
`spot-or-on-demand`, `spot` fails instead of falling back, `on-demand` never
asks. A run EC2 reclaims mid-way is an invalid row. The CSV's `market` column
records where each row actually ran, which under the default is not always
what was asked for. `just bench` and `just deploy` default to on-demand.

## Results and plots

Rows land in `results/<subject>/<name>.csv`, derived from the experiment file's
own path — moving an experiment is a rename and nothing else. The figure is
written beside the CSV.

```sh
just plot results/tpch/miniosv-sf10-query.csv
just plot results/tpch/miniosv-sf10-query.csv results/tpch/linux-sf10-query.csv \
    --series note --box --value-col query_ms --ylabel "Query latency (ms)"
```

Several CSVs are concatenated into one figure, which is how the two stacks
overlay: `--series note` splits them by the `BENCH_NOTE` each arm compiled in.
`--dark` switches the palette, `--bar` and `--box` the mark.

## Running a sweep directly

`just reproduce` is the supported path; `just bench` is the same machinery with
nothing recorded about why:

```sh
just bench apps/bench/smoltcp-s3 --sweep conns=1,2,4,8 --workers 8
just bench competitors/linux-s3 --sweep workers=1,2,4,8 --reps 1
```

The driver is `scripts/bench/<name>/bench.py`, where `<name>` is the path's
last component — `smoltcp-s3`, `duckdb-tpch`, `linux-s3` and `duckdb-linux`
have one. The `pmc-*` benches do not: they are `deploy` experiments.

## Devshells
```sh
nix develop            # extended miniosv default shell
nix develop .#cli      # extended miniosv cli shell
nix flake show         # list available shells
```

The shell loads `.env` and warns when it is missing. `just reproduce`, `just
bench` and `just plot` enter `.#rust` themselves, so they work from outside.

The submodule is consumed as a git repo (`git+file:./miniosv`), so run the
commands from the repository root, and refresh the pin after moving the
submodule to a different commit:

```sh
nix flake update miniosv
```
