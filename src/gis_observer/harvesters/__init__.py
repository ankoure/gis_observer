"""One harvester per platform. Importing this package registers all of them."""

from . import arcgis_portal, arcgis_rest, ckan, dcat, files, ogc  # noqa: F401  (registration)
from .base import Harvester

__all__ = ["Harvester"]
