"""agentalloy — runtime skill composition service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agentalloy")
except PackageNotFoundError:  # source checkout without an installed distribution
    __version__ = "0.0.0+unknown"
