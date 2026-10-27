#!/bin/sh
# Certificate renewal without Reloader: when a TLS certificate is mounted
# (Helm chart, tls.enabled), reload nginx periodically so that a certificate
# renewed by cert-manager (Secret volume updated in place by the kubelet) is
# picked up. No certificate (docker-compose, tls disabled): nothing happens.
#
# Runs from the image entrypoint as the nginx user (uid 101) before
# `exec nginx`: the loop is reparented to the nginx master (PID 1) and only
# signals it through the pid file in /tmp. Nothing is written to the
# read-only root filesystem. A reload with an unreadable or invalid
# certificate is refused by the master, which keeps serving the old one.
set -eu

cert="${LFW_TLS_CERT:-/etc/nginx/tls/tls.crt}"
interval="${LFW_TLS_RELOAD_INTERVAL:-43200}"

[ -f "$cert" ] || exit 0

case "$interval" in
    '' | *[!0-9]* | 0)
        echo "$0: invalid LFW_TLS_RELOAD_INTERVAL '$interval', periodic reload disabled" >&2
        exit 0
        ;;
esac

if [ -z "${NGINX_ENTRYPOINT_QUIET_LOGS:-}" ]; then
    echo "$0: TLS certificate found, nginx reload every ${interval}s"
fi

(
    while sleep "$interval"; do
        nginx -s reload || echo "$0: nginx -s reload failed" >&2
    done
) </dev/null &
