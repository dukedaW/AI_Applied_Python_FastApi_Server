import importlib
import pkgutil
from fastapi import APIRouter

routers = []

for module_info in pkgutil.iter_modules(__path__):
    module_name = module_info.name
    module = importlib.import_module(f"{__name__}.{module_name}")

    if hasattr(module, "router") and isinstance(module.router, APIRouter):
        routers.append(module.router)
