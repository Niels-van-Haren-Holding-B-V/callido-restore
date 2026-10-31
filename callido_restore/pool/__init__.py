"""Pool map: which bucket holds each chunk id. ``poolmap`` (v1, sequential), ``poolmap2`` (v2, tag-based), ``anchor`` (anchor
scan that feeds v2, with subprocess workers) and ``indexfetch`` (resumable download of the bucket indexes). Depends on ``core`` and ``paths`` only."""
