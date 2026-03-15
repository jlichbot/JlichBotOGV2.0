"""
sitecustomize.py
Auto-executed by Python on startup for every process in this container.
Applies the multi-source price fallback patch to fastloop_trader.
"""
try:
    import price_fallback  # noqa
except Exception:
    pass  # Never crash the main process over a patch failure
