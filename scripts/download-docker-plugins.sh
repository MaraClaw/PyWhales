#!/usr/bin/env bash

set -euo pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
compose_version=${COMPOSE_VERSION-5.6.1}
buildx_version=${BUILDX_VERSION-0.38.1}

if [[ "$(uname -s)" != Linux ]]; then
    printf 'Unsupported operating system: %s\n' "$(uname -s)" >&2
    exit 1
fi
case "$(uname -m)" in
    x86_64)
        arch=amd64
        compose_asset=docker-compose-linux-x86_64
        ;;
    aarch64)
        arch=arm64
        compose_asset=docker-compose-linux-aarch64
        ;;
    *)
        printf 'Unsupported Linux architecture: %s\n' "$(uname -m)" >&2
        exit 1
        ;;
esac

checksum_for() {
    local plugin version pin_arch digest extra checksum=
    while read -r plugin version pin_arch digest extra; do
        if [[ "$plugin" == "$1" && "$version" == "$2" && "$pin_arch" == "$3" ]]; then
            if [[ -n "$checksum" || -n "$extra" || ! "$digest" =~ ^[0-9a-f]{64}$ ]]; then
                printf 'Invalid checksum pin: %s %s %s\n' "$1" "$2" "$3" >&2
                return 1
            fi
            checksum=$digest
        fi
    done < "$script_dir/docker-plugin-checksums.txt"
    if [[ -z "$checksum" ]]; then
        printf 'No checksum pin for Linux %s: %s %s\n' "$3" "$1" "$2" >&2
        return 1
    fi
    printf '%s' "$checksum"
}

compose_checksum=$(checksum_for compose "$compose_version" "$arch")
buildx_checksum=$(checksum_for buildx "$buildx_version" "$arch")
buildx_asset="buildx-v${buildx_version}.linux-${arch}"
plugin_dir=${DOCKER_CONFIG:-"$HOME/.docker"}/cli-plugins
mkdir -p -- "$plugin_dir"
staging_dir=$(mktemp -d "$plugin_dir/.download.XXXXXX")
trap 'rm -rf -- "$staging_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wget -q "https://github.com/docker/compose/releases/download/v${compose_version}/${compose_asset}" -O "$staging_dir/docker-compose"
wget -q "https://github.com/docker/buildx/releases/download/v${buildx_version}/${buildx_asset}" -O "$staging_dir/docker-buildx"
printf '%s  %s\n' "$compose_checksum" "$staging_dir/docker-compose" "$buildx_checksum" "$staging_dir/docker-buildx" | sha256sum -c -
chmod 755 "$staging_dir/docker-compose" "$staging_dir/docker-buildx"
mv -f -- "$staging_dir/docker-compose" "$plugin_dir/docker-compose"
mv -f -- "$staging_dir/docker-buildx" "$plugin_dir/docker-buildx"
