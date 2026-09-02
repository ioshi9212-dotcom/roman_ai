from .runtime_fixes_compat import install as _install_runtime_fixes
from .scene_presence_runtime import install as _install_scene_presence

_install_runtime_fixes()
_install_scene_presence()
del _install_runtime_fixes
del _install_scene_presence
