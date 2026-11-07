"""Constants for the Flic button (flicd) integration."""

DOMAIN = "flicbutton"

DEFAULT_PORT = 5551
CONF_QUEUE_TIMEOUT = "queue_timeout"
DEFAULT_QUEUE_TIMEOUT = 3  # seconds
CONF_IGNORED_CLICK_TYPES = "ignored_click_types"
CONF_AUTO_PAIR = "auto_pair"

CLICK_SINGLE = "single"
CLICK_DOUBLE = "double"
CLICK_HOLD = "hold"
CLICK_TYPES = [CLICK_SINGLE, CLICK_DOUBLE, CLICK_HOLD]

EVENT_FLIC_CLICK = "flic_click"

CONTROLLER_STATES = ["detached", "resetting", "attached"]

BACKOFF_INITIAL = 1.0  # seconds
BACKOFF_MAX = 60.0  # seconds
PING_INTERVAL = 30.0  # seconds
PING_TIMEOUT = 10.0  # seconds
CONNECT_TIMEOUT = 10.0  # seconds


def legacy_button_name(bd_addr: str) -> str:
    """Return the legacy button name, e.g. flic_80e4da712b3c."""
    return f"flic_{bd_addr.replace(':', '').lower()}"


def signal_button_owned(entry_id: str) -> str:
    """Dispatcher signal (arg: bd_addr): the entry now owns the button's device."""
    return f"{DOMAIN}_{entry_id}_button_owned"


def signal_button_released(entry_id: str) -> str:
    """Dispatcher signal (arg: bd_addr): the button's device moved to another entry."""
    return f"{DOMAIN}_{entry_id}_button_released"
