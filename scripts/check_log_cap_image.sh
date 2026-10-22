#!/usr/bin/env bash
# Check a built Big Bad Bots starter image starts its bot through the
# platform's log-cap wrapper, and that the cap works inside the image.
#
# Usage (after `docker build -t <tag> <starter dir>`):
#
#     scripts/check_log_cap_image.sh <tag> <interpreter>
#
# <interpreter> is the ENTRYPOINT's first word: `python` for the Python
# starters, `python3` for the JavaScript one. The checks:
#
# 1. `docker inspect`: the ENTRYPOINT is exactly
#    [<interpreter>, "-I", "-S", "-u", "/opt/chipzen/log_cap.py", "--"]
#    (the form the platform's review checks) and the bot command is CMD;
# 2. the wrapper in the image is the pinned packages/common/log_cap.py, owned
#    by root and read-only (0444);
# 3. a capped run (CHIPZEN_LOG_CAP_LINES=3, a CMD that prints 6 lines) prints
#    the first 3 lines, then the `[chipzen] log capped:` marker, then nothing,
#    and exits 0.
set -euo pipefail

image="${1:?usage: check_log_cap_image.sh <image> <interpreter>}"
interpreter="${2:?usage: check_log_cap_image.sh <image> <interpreter>}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

expected="[\"${interpreter}\",\"-I\",\"-S\",\"-u\",\"/opt/chipzen/log_cap.py\",\"--\"]"
entrypoint="$(docker image inspect "$image" --format '{{json .Config.Entrypoint}}')"
[ "$entrypoint" = "$expected" ] || fail "ENTRYPOINT is $entrypoint, expected $expected"
cmd="$(docker image inspect "$image" --format '{{json .Config.Cmd}}')"
case "$cmd" in
  null | "[]") fail "the image has no CMD: the bot command belongs there" ;;
esac
echo "ENTRYPOINT OK: $entrypoint; CMD: $cmd"

pinned="$(tr -d '[:space:]' < "$root/packages/common/log_cap.py.sha256")"
in_image="$(docker run --rm --entrypoint sh "$image" -c \
  'stat -c "%U %a" /opt/chipzen/log_cap.py && sha256sum /opt/chipzen/log_cap.py')"
owner_mode="$(echo "$in_image" | sed -n 1p)"
digest="$(echo "$in_image" | sed -n 2p | cut -d' ' -f1)"
[ "$owner_mode" = "root 444" ] || fail "/opt/chipzen/log_cap.py is '$owner_mode', expected 'root 444'"
[ "$digest" = "$pinned" ] || fail "/opt/chipzen/log_cap.py is $digest, the pin is $pinned"
echo "wrapper OK: root-owned, 0444, sha256 $digest"

output="$(docker run --rm -e CHIPZEN_LOG_CAP_LINES=3 "$image" \
  sh -c 'for i in 1 2 3 4 5 6; do echo "line $i"; done')"
echo "$output"
[ "$(echo "$output" | sed -n 1,3p)" = "$(printf 'line 1\nline 2\nline 3')" ] \
  || fail "a capped run did not keep the first 3 lines"
echo "$output" | sed -n 4p | grep -q '^\[chipzen\] log capped:' \
  || fail "a capped run did not print the cap marker after line 3"
[ "$(echo "$output" | wc -l)" -eq 4 ] || fail "a capped run printed past the marker"
echo "capped run OK"
