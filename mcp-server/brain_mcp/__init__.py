"""Brain MCP server — exposes the Ai-Brain vault as memory tools over MCP."""
import os

# Single-threaded BLAS. The brain only does small dot products over stored
# vectors, but numpy's bundled OpenBLAS pre-allocates a per-thread buffer pool
# at import — ~750 MB of commit per process at its default 24 threads vs ~10 MB
# at one. The SessionStart hook spawns several brain processes at once, and on a
# commit-starved box (strixlappy, 2026-09-14) that failed the hook with
# "OpenBLAS error: Memory allocation still failed after 10 retries". Must be set
# before the first numpy/onnxruntime import. A user's own value is left alone; an
# empty one is not an override (OpenBLAS reads it as "use the default"), so it is
# replaced like an absent one.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    if not os.environ.get(_var, "").strip():
        os.environ[_var] = "1"
del _var

__version__ = "0.1.0"
