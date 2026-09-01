"""Performance helpers for AutoClipper.

Rendering workers are intentionally bounded so a small Render instance does not
fork itself into an OOM crash. The worker count can be tuned with
AUTOCLIPPER_RENDER_WORKERS.
"""
import os
from concurrent.futures import ThreadPoolExecutor


def render_workers() -> int:
    configured = int(os.getenv("AUTOCLIPPER_RENDER_WORKERS", "2"))
    return max(1, min(configured, 4))


def parallel_render(items, render_one):
    """Render independent clips concurrently while preserving result order."""
    if len(items) <= 1:
        return [render_one(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(render_workers(), len(items))) as pool:
        return list(pool.map(render_one, items))
