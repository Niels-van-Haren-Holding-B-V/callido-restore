"""Compatibility shim: this module moved to ``callido_restore.core.pool`` (H16 step 1). Importing ``hbk_pool`` returns that
module object itself, so ``import hbk_pool`` and ``from hbk_pool import X`` keep working until the shims are removed."""
import sys
from callido_restore.core import pool as _pool
sys.modules[__name__] = _pool
