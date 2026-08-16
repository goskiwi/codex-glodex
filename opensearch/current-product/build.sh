#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="/home/sybai/miniforge3/envs/glodex/bin/python3.11"
binding_file="$root_dir/current_product_binding.json"
default_runtime_dir="/data3/sybai/glodex/product-search/runtime"
runtime_dir="${OPENSEARCH_RUNTIME_DIR:-$default_runtime_dir}"
service_pid=""

usage() {
  echo "Usage: $0 [--runtime-dir PATH] {validate-assets|build|verify}" >&2
}

operation=""
while (( $# > 0 )); do
  case "$1" in
    --runtime-dir)
      if (( $# < 2 )) || [[ -z "$2" ]]; then
        usage
        exit 64
      fi
      runtime_dir="$2"
      shift 2
      ;;
    validate-assets|build|verify)
      if [[ -n "$operation" ]]; then
        usage
        exit 64
      fi
      operation="$1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 64
      ;;
  esac
done

if [[ -z "$operation" ]]; then
  usage
  exit 64
fi

if [[ "$runtime_dir" != /* ]]; then
  echo "OpenSearch runtime path must be absolute: $runtime_dir" >&2
  exit 64
fi
export OPENSEARCH_RUNTIME_DIR="${runtime_dir%/}"

stop_service() {
  exit_code=$?
  trap - EXIT INT TERM
  if [[ -n "$service_pid" ]] && kill -0 "$service_pid" 2>/dev/null; then
    kill -TERM "$service_pid"
    for ((attempt = 1; attempt <= 60; attempt++)); do
      if ! kill -0 "$service_pid" 2>/dev/null; then
        break
      fi
      sleep 1
    done
  fi
  if [[ -n "$service_pid" ]] && kill -0 "$service_pid" 2>/dev/null; then
    echo "OpenSearch did not stop after 60 seconds; terminating only pid $service_pid" >&2
    kill -KILL "$service_pid"
  fi
  wait "$service_pid" 2>/dev/null || true
  exit "$exit_code"
}

wait_for_service() {
  for ((attempt = 1; attempt <= 90; attempt++)); do
    if curl -fsS 'http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s' >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "OpenSearch did not become ready; see $root_dir/logs/opensearch-console.log" >&2
  return 1
}

run_operator() {
  "$python_bin" "$root_dir/build_current_product_index.py" --binding "$binding_file" "$@" \
    2>&1 | tee "$root_dir/logs/operator.log"
}

if [[ "$operation" == "validate-assets" ]]; then
  "$python_bin" "$root_dir/build_current_product_index.py" \
    --binding "$binding_file" --action validate-assets
  exit 0
fi

mkdir -p "$root_dir/logs"
if curl --max-time 1 -fsS 'http://127.0.0.1:9200/' >/dev/null 2>&1; then
  echo "127.0.0.1:9200 is already in use; refusing to use an existing service" >&2
  exit 1
fi

"$root_dir/start.sh" >"$root_dir/logs/opensearch-console.log" 2>&1 &
service_pid=$!
trap stop_service EXIT INT TERM
wait_for_service

case "$operation" in
  build)
    run_operator --action build
    ;;
  verify)
    run_operator --action verify
    ;;
  *)
    usage
    exit 64
    ;;
esac
