"""
Light cross-app signals. Currently a no-op — lifecycle bookkeeping lives in
services.py so callers go through explicit helpers. This module exists so
tickets.apps.TicketsConfig.ready() can hook signals later without touching
app config again.
"""
