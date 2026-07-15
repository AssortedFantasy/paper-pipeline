"""Job progress events.

Implemented by WP-2D.1. A simple in-process pub/sub of job
state transitions and progress messages. The web layer subscribes and
forwards events over SSE; the event bus itself knows nothing about HTTP.
"""
