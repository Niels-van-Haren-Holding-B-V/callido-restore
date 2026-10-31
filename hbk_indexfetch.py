"""Compatibility shim: this module moved to ``callido_restore.pool.indexfetch`` (H16 step 2). Importing ``hbk_indexfetch`` returns that
module object itself, so ``import hbk_indexfetch`` and ``from hbk_indexfetch import X`` keep working until the shims are removed."""
import sys
from callido_restore.pool import indexfetch as _m
sys.modules[__name__] = _m
