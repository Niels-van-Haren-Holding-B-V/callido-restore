#!/bin/sh
# DSM runs this for /webman/3rdparty/callido-restore/index.cgi/...; the package's own Python does the work (dsm_cgi.py).
exec /var/packages/callido-restore/target/env/bin/python3 /var/packages/callido-restore/target/app/dsm_cgi.py
