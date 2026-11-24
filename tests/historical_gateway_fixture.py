"""Load immutable predecessor gateway source independently of product modules."""
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tests/fixtures/runtime_gateway_predecessor'
MANIFEST = json.loads((SOURCE / 'source.json').read_text())
FROZEN = frozenset(Path(name).stem for name in MANIFEST['files'])
for name, digest in MANIFEST['files'].items():
    if hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() != digest:
        raise RuntimeError('Historical gateway test source changed: ' + name)


class _DependencyLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = importlib.import_module('agent_coord.' + spec.name.split('.', 1)[1])
        self.original = {name: getattr(module, name, None) for name in ('__name__', '__package__', '__loader__', '__spec__')}
        return module

    def exec_module(self, module):
        for name, value in self.original.items():
            setattr(module, name, value)


class _DependencyFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('historical_gateway.') and fullname.split('.', 1)[1] not in FROZEN:
            return importlib.util.spec_from_loader(fullname, _DependencyLoader())
        return None


if 'historical_gateway' not in sys.modules:
    package = types.ModuleType('historical_gateway')
    package.__path__ = [str(SOURCE)]
    package.__package__ = 'historical_gateway'
    sys.modules['historical_gateway'] = package
    sys.meta_path.insert(0, _DependencyFinder())


def copy_to(package):
    """Overlay predecessor engines in a private fixture payload, never product."""
    for name in MANIFEST['files']:
        shutil.copyfile(SOURCE / name, package / name)
