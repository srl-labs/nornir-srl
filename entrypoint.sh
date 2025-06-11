#!/usr/bin/env sh
if [ "$#" -eq 0 ]; then
    exec /bin/sh
else
    exec fcli "$@"
fi
