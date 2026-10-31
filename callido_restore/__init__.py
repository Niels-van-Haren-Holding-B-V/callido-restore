"""Callido Restore: browse a Synology Hyper Backup (.hbk) and restore files from it.

Layers (H16), bottom up: ``paths``/``about``, ``core`` (the .hbk format: pool streams, bucket indexes, chunk records, file
reconstruction), ``pool`` (pool map v1/v2 and index fetch), ``sources`` (local folders and rclone remotes, read-only), ``restore``
(plan, destinations, no-overwrite writer, jobs, ``Backup``), ``server`` (local HTTP API) and ``app`` (desktop window and entry
point). A layer never imports a higher one.
"""
