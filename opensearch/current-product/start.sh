#!/usr/bin/env bash
set -euo pipefail

default_runtime_dir="/data3/sybai/glodex/product-search/runtime"
runtime_dir="${OPENSEARCH_RUNTIME_DIR:-$default_runtime_dir}"
config_dir="$runtime_dir/config"
service_root="/data3/sybai/glodex/product-search"

usage() {
  echo "Usage: $0 [--runtime-dir PATH]" >&2
}

while (( $# > 0 )); do
  case "$1" in
    --runtime-dir)
      if (( $# < 2 )) || [[ -z "$2" ]]; then
        usage
        exit 64
      fi
      runtime_dir="$2"
      config_dir="$runtime_dir/config"
      shift 2
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

if [[ "$runtime_dir" != /* ]]; then
  echo "OpenSearch runtime path must be absolute: $runtime_dir" >&2
  exit 64
fi

if [[ ! -x "$runtime_dir/bin/opensearch" ]]; then
  echo "Missing official OpenSearch runtime: $runtime_dir/bin/opensearch" >&2
  exit 1
fi
if [[ ! -f "$config_dir/jvm.options" || ! -f "$config_dir/log4j2.properties" ]]; then
  echo "Official OpenSearch runtime config is incomplete: $config_dir" >&2
  exit 1
fi

if curl --max-time 1 -fsS 'http://127.0.0.1:9200/' >/dev/null 2>&1; then
  echo "127.0.0.1:9200 is already in use; refusing to use an existing service" >&2
  exit 1
fi

# Reuse the server's official runtime and its config in place.  Do not copy or
# overwrite that config: the command-line settings below define this temporary,
# loopback-only operator instance.  There is no project-local OpenSearch config.
mkdir -p "$service_root/data" "$service_root/logs"
ulimit -n 65535
export OPENSEARCH_PATH_CONF="$config_dir"
export OPENSEARCH_JAVA_OPTS="${OPENSEARCH_JAVA_OPTS:--Xms16g -Xmx16g}"

exec "$runtime_dir/bin/opensearch" \
  -Ecluster.name=glodex-current-product \
  -Enode.name=current-product \
  -Epath.data="$service_root/data" \
  -Epath.logs="$service_root/logs" \
  -Enetwork.host=127.0.0.1 \
  -Ehttp.port=9200 \
  -Etransport.port=9300 \
  -Ediscovery.type=single-node \
  -Enode.store.allow_mmap=false \
  -Ebootstrap.memory_lock=false \
  -Eplugins.security.disabled=true
