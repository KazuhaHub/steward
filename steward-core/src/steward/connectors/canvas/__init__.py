"""The Canvas LMS connector."""

from steward.connectors import Connector

from .probe import probe_token

CONNECTOR = Connector(id="canvas", display_name="Canvas LMS", probe=probe_token)
