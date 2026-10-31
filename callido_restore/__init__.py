"""Callido Restore: browse a Synology Hyper Backup (.hbk) and restore files from it.

Layers (H16): ``core`` (the .hbk format: pool streams, bucket indexes, chunk records, file reconstruction), ``pool`` (pool map
v1/v2 and index fetch), ``restore`` (plan, destinations, no-overwrite writer, jobs), ``sources`` (local folders and rclone
remotes, read-only), ``server`` (local HTTP API) and ``app`` (desktop window and entry point). Lower layers never import higher ones.
"""
