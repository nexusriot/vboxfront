#!/usr/bin/env bash
# Build and run the hermetic end-to-end suite in Docker.
#
#   e2e/run.sh              # unit suite + e2e suite
#   e2e/run.sh e2e          # e2e suite only
#   e2e/run.sh shell        # a prompt inside the image
#
# No VirtualBox, no display, no network at run time: the container talks to
# e2e/fake_vboxmanage.py, and `--network none` keeps that honest.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="${VBOXFRONT_E2E_IMAGE:-vboxfront-e2e}"

echo "==> building $IMAGE"
# Not -q: the first build pulls a base image and a ~100 MB PyQt6 wheel, and
# several silent minutes look identical to a hang.
docker build -f e2e/Dockerfile -t "$IMAGE" .

run() {
  docker run --rm --network none -t "$IMAGE" "$@"
}

case "${1:-all}" in
  all)   echo "==> unit + e2e"; run ;;
  unit)  echo "==> unit only"
         run sh -c "python -m unittest discover -s tests -t . -v" ;;
  e2e)   echo "==> e2e only"
         run sh -c "python -m unittest discover -s e2e -t . -v" ;;
  shell) docker run --rm --network none -it "$IMAGE" sh ;;
  *)     echo "usage: $0 [all|unit|e2e|shell]" >&2; exit 2 ;;
esac
