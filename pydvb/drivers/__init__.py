"""Built-in DVB device drivers and driver discovery."""

from importlib import import_module
from inspect import isabstract
from pkgutil import iter_modules

from ..driver_api.device import DVBDevice


def discover_device_types() -> tuple[type[DVBDevice], ...]:
    """Return concrete DVB device classes exported by driver subpackages.

    Each driver subpackage lists its public classes in ``__all__``. Only those
    exports are inspected, so helper classes in a driver's implementation
    modules are not treated as independently supported devices.
    """
    device_types: list[type[DVBDevice]] = []
    for package_info in sorted(iter_modules(__path__), key=lambda item: item.name):
        if not package_info.ispkg:
            continue

        driver_package = import_module(f"{__name__}.{package_info.name}")
        for export_name in getattr(driver_package, "__all__", ()):
            exported = getattr(driver_package, export_name)
            if (
                not isinstance(exported, type)
                or not issubclass(exported, DVBDevice)
                or isabstract(exported)
                or exported in device_types
            ):
                continue
            device_types.append(exported)

    return tuple(device_types)


__all__ = ["discover_device_types"]