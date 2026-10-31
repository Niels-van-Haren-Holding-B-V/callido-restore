"""Compatibility shim: this module moved to ``callido_restore.pool.poolmap2`` (H16 step 2). Importing ``hbk_poolmap2`` returns that
module object itself, so ``import hbk_poolmap2`` and ``from hbk_poolmap2 import X`` keep working until the shims are removed."""
import sys
from callido_restore.pool import poolmap2 as _m
sys.modules[__name__] = _m
