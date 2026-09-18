# Shared setup for the benches that use S3. Source it; do not run it.
#
#     . "$(dirname "$0")/../../scripts/bench-setup.sh"   # or wherever it lands
#
# Every bench that reads from the bucket needs the same four things before it
# can do anything of its own: the root .env, an AWS login, the VPC endpoint the
# guests read through, and a bucket-policy grant for whatever it just uploaded.
# Six justfiles used to carry their own copy of each. The bench-specific part --
# what to generate, what to upload, under which prefix -- stays in the bench.
#
# The root is derived from this file's own location rather than counted out in
# parent_directory() calls, which is what made the old copies differ: three
# levels from apps/bench/<x>/, two from competitors/<x>/, and a `root` variable
# in the two that had been moved.

# Resolve even when sourced through a relative path or a symlink.
BENCH_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_ROOT="$(dirname "$BENCH_LIB")"
BENCH_ENV="$BENCH_ROOT/.env"

bench_die() { echo "$*" >&2; exit 1; }

# Log in only if the current credentials are not already good: `aws login` is
# interactive and a sweep that re-runs setup should not stop to ask.
bench_login() {
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        echo "not logged in to aws -- running 'aws login --remote'"
        aws login --remote
    fi
}

# Load the root .env into the environment.
#
#   bench_env_load            fail with an explanation if it is not there yet
#   bench_env_load bootstrap  run smoltcp-s3's setup to create it, then load
#
# smoltcp-s3 owns the bucket, the subnet and the gateway endpoint; every other
# bench reuses them rather than making a second set. That is why bootstrapping
# means "run that one first" and not "do it again here".
bench_env_load() {
    if [ ! -f "$BENCH_ENV" ] || ! grep -q '^AWS_BUCKET=' "$BENCH_ENV"; then
        if [ "${1-}" = "bootstrap" ]; then
            echo "no bucket on record yet -- provisioning one via smoltcp-s3's setup"
            # with_blob=0: that bench's 10 GiB random blob is its own workload,
            # and a caller bootstrapping the bucket for its own data should not
            # wait for, or pay for, an upload it will never read. Running
            # smoltcp-s3's own setup later adds the blob.
            just --justfile "$BENCH_ROOT/apps/bench/smoltcp-s3/justfile" \
                 --working-directory "$BENCH_ROOT/apps/bench/smoltcp-s3" setup 0
        else
            bench_die "no $BENCH_ENV -- run 'just setup apps/bench/smoltcp-s3' first.
That recipe owns the bucket and the gateway endpoint; this one only adds to them."
        fi
    fi
    set -a
    # shellcheck disable=SC1090
    . "$BENCH_ENV"
    set +a
    : "${AWS_BUCKET:?no AWS_BUCKET in $BENCH_ENV}"
    : "${AWS_REGION:?no AWS_REGION in $BENCH_ENV}"
    bench_region_check
}

# Refuse a bucket that is not in $AWS_REGION. The guests dial
# <bucket>.s3.<region>.amazonaws.com through a gateway endpoint that exists
# in one region only, so a mismatch would either 403 at the bucket policy or,
# with a looser policy, bill every byte as cross-region transfer. Checked
# wherever a bucket name and a region first meet; scripts/bench/runner.py
# repeats it before every sweep.
bench_region_check() {
    local where
    where=$(aws s3api get-bucket-location --bucket "$AWS_BUCKET" \
        --query LocationConstraint --output text 2>/dev/null) \
        || bench_die "cannot read the region of s3://$AWS_BUCKET -- wrong account, or no such bucket"
    case "$where" in
        None|null|"") where=us-east-1 ;;   # the API's spelling of us-east-1
        EU) where=eu-west-1 ;;             # legacy spelling
    esac
    [ "$where" = "$AWS_REGION" ] || bench_die \
        "s3://$AWS_BUCKET is in $where, but AWS_REGION is $AWS_REGION -- every byte would cross regions. Fix .env or move the bucket."
}

# The S3 gateway endpoint the guests read through, on stdout.
#
#   bench_vpce           fail if there is none
#   bench_vpce --create  make one in the default VPC's main route table
bench_vpce() {
    local vpce
    vpce=$(aws ec2 describe-vpc-endpoints \
        --filters "Name=service-name,Values=com.amazonaws.$AWS_REGION.s3" \
                  "Name=vpc-endpoint-type,Values=Gateway" \
        --query "VpcEndpoints[0].VpcEndpointId" --output text 2>/dev/null || true)

    if [ -z "$vpce" ] || [ "$vpce" = "None" ]; then
        if [ "${1-}" != "--create" ]; then
            bench_die "no S3 gateway endpoint in $AWS_REGION -- run 'just setup apps/bench/smoltcp-s3' first"
        fi
        local vpc rtb
        vpc=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
            --query "Vpcs[0].VpcId" --output text)
        rtb=$(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$vpc" \
            --query "RouteTables[?Associations[?Main]].RouteTableId | [0]" --output text)
        echo "creating an s3 gateway endpoint in $vpc" >&2
        vpce=$(aws ec2 create-vpc-endpoint --vpc-id "$vpc" \
            --service-name "com.amazonaws.$AWS_REGION.s3" --route-table-ids "$rtb" \
            --query "VpcEndpoint.VpcEndpointId" --output text)
    fi
    printf '%s' "$vpce"
}

# Grant the endpoint read access to one or more keys, under a Sid of this
# bench's own. Merged by Sid rather than written wholesale, because several
# benches share the bucket and each owns only its own statement.
#
#     bench_grant TpchSf1FromVpce "arn:aws:s3:::$AWS_BUCKET/tpch/sf1/*"
bench_grant() { bench_policy add "$@"; }

# add|remove, for a bench whose recipe is parameterised over the two.
#
#     bench_policy "{{ action }}" BenchBinFromVpce "arn:aws:s3:::$AWS_BUCKET/bin/*"
bench_policy() {
    local action="$1" sid="$2"; shift 2
    local args=()
    for r in "$@"; do args+=(--resource "$r"); done
    AWS_BUCKET="$AWS_BUCKET" python3 "$BENCH_ROOT/scripts/bucket_policy.py" \
        "$action" --sid "$sid" --vpce "$(bench_vpce)" "${args[@]}"
}

# Fail unless every named variable is set, naming the missing one and where it
# should have come from. A bench that needs more than the bucket and region --
# the subnet to launch in, the shape a sweep was configured with -- says so.
#
#     bench_env_require AWS_SUBNET BENCH_WORKERS
bench_env_require() {
    for v in "$@"; do
        [ -n "${!v:-}" ] || bench_die \
            "$v missing from $BENCH_ENV -- re-run 'just setup apps/bench/smoltcp-s3'"
    done
}

bench_revoke() { bench_policy remove "$1"; }
