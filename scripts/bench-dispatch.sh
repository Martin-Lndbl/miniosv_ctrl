#!/usr/bin/env bash
# Forward `just setup|clean <bench>` to that bench's own justfile.
#
# Exists so a mistyped bench says which one it looked for and lists the ones
# that exist, instead of just's own message about a missing file:
#
#     error: failed to read justfile at `.../duckdb-tcph/justfile`: No such file
#
# which names a path the caller never typed and offers nothing to do about it.
set -euo pipefail

recipe="$1"; app="$2"; shift 2
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dir="$root/${app%/}"

if [ ! -f "$dir/justfile" ]; then
    echo "no bench at '$app' (looked for $dir/justfile)" >&2
    echo >&2
    echo "benches with a $recipe recipe:" >&2
    while IFS= read -r f; do
        grep -q "^$recipe" "$f" || continue
        printf '  %s\n' "$(dirname "${f#"$root"/}")" >&2
    done < <(find "$root/apps" "$root/competitors" -name justfile 2>/dev/null | sort)
    exit 1
fi

exec just --justfile "$dir/justfile" --working-directory "$dir" "$recipe" "$@"
