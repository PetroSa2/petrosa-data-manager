#!/usr/bin/env bash
# De-networked gitleaks pre-commit hook (petrosa_k8s#1043 §3 / #1052 epic step 0.2).
#
# The upstream `repo: https://github.com/gitleaks/gitleaks` pre-commit hook form
# is `language: golang`, which makes pre-commit run `go install ./...` on a cold
# cache. That build fetches Go module dependencies from proxy.golang.org, and a
# transient network flake there (observed in petrosa-data-manager CI runs
# 34619481365 / 34624977944, both at the "Install pre-commit" step) fails the
# entire blocking pre-commit gate even though there is no secret-scanning
# defect. This script fetches the official prebuilt gitleaks binary (the same
# release asset CI's own "Run Gitleaks" security-scan step already uses)
# instead of building from source, removing the go-module-proxy dependency
# from the blocking PR-path gate entirely.
set -euo pipefail

GITLEAKS_VERSION="8.18.4"
CACHE_DIR="${HOME}/.cache/petrosa-gitleaks/${GITLEAKS_VERSION}"
BIN_PATH="${CACHE_DIR}/gitleaks"

if [ ! -x "${BIN_PATH}" ]; then
  OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
  ARCH="$(uname -m)"
  case "${ARCH}" in
    x86_64|amd64) ARCH="x64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "run-gitleaks-precommit.sh: unsupported arch '${ARCH}'" >&2; exit 1 ;;
  esac

  mkdir -p "${CACHE_DIR}"
  ASSET="gitleaks_${GITLEAKS_VERSION}_${OS}_${ARCH}.tar.gz"
  URL="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${ASSET}"
  curl -sSL "${URL}" | tar -xz -C "${CACHE_DIR}" gitleaks
  chmod +x "${BIN_PATH}"
fi

exec "${BIN_PATH}" protect --verbose --redact --staged "$@"
