#!/bin/sh
# On-demand verified backup (same code path as the nightly maintenance timer).
exec /usr/local/bin/newsrelay backup "$@"
