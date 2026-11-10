"""Constants for the BOLOLO integration."""

from homeassistant.const import Platform

DOMAIN = "bololo"
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
]
CONF_COUNTRY_CODE = "country_code"
CONF_CONNECTION_MODE = "connection_mode"
CONF_LAN_HOSTS = "lan_hosts"
CONF_LAN_PASSCODES = "lan_passcodes"
CONF_DEVICES = "devices"
DEFAULT_CONNECTION_MODE = "auto"
CONNECTION_MODES = ("auto", "cloud", "lan")
DEFAULT_SCAN_INTERVAL = 30
