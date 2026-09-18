"""Keep Linux Fcitx and its Qt runtime together for source launches."""
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path


def select_qt_runtime_for_input_method():
    """Prefer an installed system PyQt5 when bundled Qt lacks Fcitx.

    Fcitx uses Qt private APIs: copying a system plugin into a different Qt
    patch version can abort the process. Select the complete system PyQt5
    package instead, retaining the project's other virtualenv dependencies.
    No packages, desktop settings or system files are changed.
    """
    if (not sys.platform.startswith('linux') or getattr(sys, 'frozen', False)
            or 'PyQt5.QtCore' in sys.modules
            or os.environ.get('QT_IM_MODULE', '').lower() not in ('fcitx', 'fcitx5')):
        return False
    spec = importlib.util.find_spec('PyQt5')
    if not spec or not spec.origin:
        return False
    current = Path(spec.origin).parent
    if list((current / 'Qt5/plugins/platforminputcontexts').glob('*fcitx*platforminputcontextplugin.so')):
        return False
    system = Path('/usr/lib/python3/dist-packages/PyQt5')
    if system == current or not (system / '__init__.py').is_file():
        return False
    # Only use binaries compatible with the current Python interpreter.
    if not all(any((system / (name + suffix)).is_file()
                   for suffix in importlib.machinery.EXTENSION_SUFFIXES)
               for name in ('QtCore', 'QtGui', 'QtWidgets', 'sip')):
        return False
    plugins = list(Path('/usr/lib').glob('*/qt5/plugins/platforminputcontexts/*fcitx*platforminputcontextplugin.so'))
    if not plugins:
        return False
    system_spec = importlib.util.spec_from_file_location('PyQt5', system / '__init__.py',
                                                       submodule_search_locations=[str(system)])
    package = importlib.util.module_from_spec(system_spec)
    sys.modules['PyQt5'] = package
    system_spec.loader.exec_module(package)
    return True
