"""AmberGate environment settings with compatibility for existing installations."""
import os


def setting(name, default=None):
    """The AMBERGATE_ setting wins, including an explicitly empty value."""
    key = "AMBERGATE_" + name
    if key in os.environ:
        return os.environ[key]
    return os.environ.get("GATEWAY_" + name, default)
