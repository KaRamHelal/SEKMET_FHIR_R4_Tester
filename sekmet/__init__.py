"""SEKMET FHIR R4 Tester."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sekmet-fhir")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0+unknown"
