#!/usr/bin/env bash
# Run an Inferno test kit against SEKMET with the lightest setup: one rootless podman (or docker) container,
# no compose stack, no web UI, no Redis. Gems are cached in the volume "inferno-gems".
#
#   scripts/inferno.sh setup [kit-repo]         clone kit (default smart-app-launch-test-kit) + install gems
#   scripts/inferno.sh run <inferno args...>    e.g. execute --suite smart_stu2_2 --groups 3 --inputs ...
#
# Env: INFERNO_DIR (default .inferno/<kit>), SEKMET_CA (PEM to trust, for SEKMET served over self-signed TLS),
#      ENGINE (podman|docker, default podman if present).
set -euo pipefail
KIT_REPO="${2:-https://github.com/inferno-framework/smart-app-launch-test-kit.git}"
KIT_NAME="$(basename "$KIT_REPO" .git)"
INFERNO_DIR="${INFERNO_DIR:-.inferno/$KIT_NAME}"
ENGINE="${ENGINE:-$(command -v podman >/dev/null && echo podman || echo docker)}"
IMAGE="docker.io/library/ruby:3.3.6-slim"
CA_ARGS=()
if [[ -n "${SEKMET_CA:-}" ]]; then
  CA_ARGS=(-v "$(realpath "$SEKMET_CA"):/certs/ca.crt:ro,Z" -e SSL_CERT_FILE=/certs/ca.crt)
fi
run() {
  "$ENGINE" run --rm --network host -v "$(realpath "$INFERNO_DIR"):/kit:Z" -v inferno-gems:/usr/local/bundle \
    -w /kit "${CA_ARGS[@]}" "$IMAGE" "$@"
}
case "${1:-}" in
  setup)
    [[ -d "$INFERNO_DIR" ]] || git clone -q --depth 1 "$KIT_REPO" "$INFERNO_DIR"
    # build tools only for this one-off install; the compiled gems persist in the volume
    run bash -c 'apt-get update -qq >/dev/null && apt-get install -y -qq build-essential libyaml-dev libpq-dev git \
      >/dev/null 2>&1; bundle install -j4 | tail -2 && bundle exec inferno migrate >/dev/null 2>&1 && echo ready'
    ;;
  run)
    shift; run bundle exec inferno "$@" 2> >(grep -v "git: not found" >&2)
    ;;
  *) sed -n 2,11p "$0"; exit 1 ;;
esac
