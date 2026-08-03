"""Easting QGIS plugin entry point (Easting Deeds)."""


def classFactory(iface):  # noqa: N802 (QGIS-mandated name)
    from .plugin import EastingPlugin

    return EastingPlugin(iface)
