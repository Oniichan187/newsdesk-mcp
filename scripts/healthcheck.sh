#!/bin/sh
# Manual end-to-end health check: HTTP /healthz + detailed status. Exit 0 = healthy.
/usr/local/bin/newsrelay healthcheck || { echo "UNHEALTHY"; exit 1; }
/usr/local/bin/newsrelay status
