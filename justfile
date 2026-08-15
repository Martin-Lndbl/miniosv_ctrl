set dotenv-load := true
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

# Run a bench's sweep, e.g. 'just bench smoltcp-s3 --sweep conns=1,2,4,8'
bench name *args:
    nix develop .#rust --command python3 \
        "{{ justfile_directory() }}/scripts/bench/{{ name }}/bench.py" {{ args }}
