"""Configuration loading for the mini order service."""

DEFAULTS = {"currency": "USD", "discount_rate": 0.1}


def parse_line(line):
    """Split one ``key=value`` config line into a (key, value) pair."""
    key, _, value = line.partition("=")
    return key.strip(), value.strip()


def load_config(path):
    """Parse a configuration file into a settings dictionary.

    Reads ``key=value`` lines, skips comments, and overlays the parsed
    values on top of DEFAULTS.
    """
    settings = dict(DEFAULTS)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, value = parse_line(line)
            settings[key] = value
    return settings
