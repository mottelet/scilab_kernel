import sys

from metakernel import __version__ as mversion

from . import __version__
from .kernel import ScilabKernel

if __name__ == "__main__":
    print(f'Scilab kernel v{__version__}')
    print(f'Metakernel v{mversion}')
    print(f'Python v{sys.version}')
    print(f'Python path: {sys.executable}')
    print('\nConnecting to Scilab...')
    try:
        s = ScilabKernel()
        print('Scilab connection established')
        print(s.banner)
    except Exception as e:  # noqa: BLE001 -- diagnostic script: report any failure, don't crash with a raw traceback
        print(e)
