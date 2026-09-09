set dotenv-load := true
# So shell recipes can forward "$@" with quoting intact; without it
# `--title "a b"` reaches the script as two arguments.
set positional-arguments
set shell := ["bash", "-euo", "pipefail", "-c"]
miniosv := justfile_directory() / "miniosv"

# List available recipes
default:
    @just --list

# Run a bench's setup, e.g. 'just setup apps/bench/smoltcp-s3'
setup app:
    just --justfile "{{ absolute_path(app) }}/justfile" --working-directory "{{ absolute_path(app) }}" setup

# Delete a bench's bucket and .env, e.g. 'just clean apps/bench/smoltcp-s3' ('force' skips the prompt)
clean app *force:
    just --justfile "{{ absolute_path(app) }}/justfile" --working-directory "{{ absolute_path(app) }}" clean {{ force }}

# Build the boot image against an app; extra args go to make (-j8, arch=aarch64, …)
build app *args:
    make -C "{{ miniosv }}" app="{{ absolute_path(app) }}" -j {{ args }}

# Boot the image under QEMU; extra args go to run.py (--arch aarch64, -m 4G, …)
run *args:
    "{{ miniosv }}/scripts/run.py" {{ args }}

# Deploy the image to EC2 in $AWS_SUBNET; extra args go to aws-deploy.py
deploy instance *args:
    cd "{{ miniosv }}" && "./scripts/aws-deploy.py" "$AWS_REGION" "{{ instance }}" \
        --attach --subnet "$AWS_SUBNET" {{ args }}

# Reproduce a stored experiment end to end, e.g. 'just reproduce conns-plateau'
reproduce name *args:
    #!/usr/bin/env bash
    set -euo pipefail
    nix develop "{{ justfile_directory() }}#rust" --command python3 \
        "{{ justfile_directory() }}/scripts/bench/experiment.py" "$@"

# List stored experiments, grouped by the question they answer
experiments:
    @find "{{ justfile_directory() }}/experiments" -name '*.toml' | sort \
        | xargs grep -H '^title' \
        | sed 's|.*/experiments/||; s|\.toml:title *= *"| — |; s|"$||'

# Plot a sweep CSV, e.g. 'just plot smoltcp-s3' or 'just plot results/x/sweep.csv --dark'
plot csv *args:
    #!/usr/bin/env bash
    set -euo pipefail
    nix develop "{{ justfile_directory() }}#rust" --command python3 \
        "{{ justfile_directory() }}/scripts/bench/plot.py" "$@"

# Run a bench's sweep, e.g. 'just bench apps/bench/smoltcp-s3 --sweep conns=1,2,4,8'
bench app *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # Takes a path like every other recipe here; the driver is
    # scripts/bench/<name>/bench.py where <name> is the path's last component.
    driver="{{ justfile_directory() }}/scripts/bench/{{ file_name(app) }}/bench.py"
    if [ ! -f "$driver" ]; then
        echo "no sweep driver for {{ app }} (looked for $driver)" >&2
        exit 1
    fi
    nix develop "{{ justfile_directory() }}#rust" --command python3 "$driver" "${@:2}"

# Rerun the pmc-cost experiment end to end and replot it.
#   just reproduce-pmc                          # 3 boots on all three vendors
#   just reproduce-pmc 5 "c7i.large c8g.large"  # boots, machines
reproduce-pmc *args:
    #!/usr/bin/env bash
    set -euo pipefail
    just --justfile "{{ justfile_directory() }}/apps/bench/pmc-cost/justfile" \
        --working-directory "{{ justfile_directory() }}/apps/bench/pmc-cost" \
        reproduce "$@"
