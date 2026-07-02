"""Compatibility facade for proxy request orchestration.

File ownership after the proxy-core split:

- ``proxy_core.py``: legacy facade only. It aliases this module name to
  ``proxy.core`` so old imports, monkeypatch paths, and module-level state
  assignments keep working. Do not add new implementation here.
- ``proxy/core.py``: request orchestration and compatibility wrappers; keeps
  ``proxy_request()``, model/model-group fallback, one-shot request execution,
  stream execution, request logging, and public/private names historically
  imported from ``proxy_core``.
- ``proxy/channel_registry.py``: enabled-channel lookup by model, model-channel
  cache state, storage save callback, and load-balancer cleanup for removed
  channels.
- ``proxy/conversion.py``: API-format routing table, converter selection,
  cross-format channel filtering, and Responses ``previous_response_id``
  history expansion for non-Responses upstreams.
- ``proxy/stream_sse.py``: SSE block parsing, SSE formatting, Anthropic event
  synthesis, and non-SSE JSON-to-stream fallback helpers.
- ``proxy/stream_reconstruct.py``: rebuild full response bodies from captured
  stream chunks for request logging.
- ``proxy/errors.py``, ``proxy/routing.py``, ``proxy/non_stream_executor.py``,
  ``proxy/stream_executor.py``, and ``proxy/media.py``: narrow re-export modules
  that make the intended boundaries explicit while preserving the old surface.

The implementation lives in :mod:`proxy.core`. Importing ``proxy_core`` returns
that implementation module so legacy monkeypatch paths and module-level state
assignments keep affecting the live code.
"""

import sys

from proxy import core as _core

sys.modules[__name__] = _core
