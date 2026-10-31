"""Compatibility shim: this module moved to ``callido_restore.pool.poolmap`` (H16 step 2). Importing ``hbk_poolmap`` returns that
module object itself, so ``import hbk_poolmap`` and ``from hbk_poolmap import X`` keep working until the shims are removed."""
import sys
from callido_restore.pool import poolmap as _m
sys.modules[__name__] = _m
