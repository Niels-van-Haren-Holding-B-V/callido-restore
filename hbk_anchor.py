"""Compatibility shim: this module moved to ``callido_restore.pool.anchor`` (H16 step 2). Importing ``hbk_anchor`` returns that
module object itself, so ``import hbk_anchor`` and ``from hbk_anchor import X`` keep working until the shims are removed."""
import sys
from callido_restore.pool import anchor as _m
sys.modules[__name__] = _m
