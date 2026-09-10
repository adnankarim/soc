"""A minimal, dependency-free MCP-compatible server over JSON-RPC stdio.

It implements the useful MCP subset for this exercise:

* ``initialize`` and ``notifications/initialized``
* ``ping``
* ``tools/list``
* ``tools/call``

One JSON-RPC message is read from each stdin line and one response (when a
request has an id) is written to stdout.  Diagnostic messages must go to stderr
so they cannot corrupt the protocol stream.  The service behind this transport
is the same one used by ``soc_explorer.adapter``.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from .service import DEFAULT_DESIGN, ExplorerService


SERVER_NAME = "orion-soc-explorer-easy"
SERVER_VERSION = "1.0.0"
# These versions cover common MCP hosts while keeping the implementation small.
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[-1]