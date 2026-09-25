"""Example use of jupyter_kernel_test, with tests for IPython."""

import unittest
from typing import ClassVar

import jupyter_kernel_test as jkt


class ScilabKernelTests(jkt.KernelTests):
    kernel_name = "scilab"

    language_name = "scilab"

    code_hello_world = "disp('hello, world')"

    code_stderr = "zzzzzzz_undefined_variable"

    code_display_data: ClassVar[list] = [
        {'code': '%plot -f png\nplot([1,2,3])', 'mime': 'image/png'},
        {'code': '%plot -f svg\nplot([1,2,3])', 'mime': 'image/svg+xml'}
    ]

    completion_samples: ClassVar[list] = [
        {
            'text': 'one',
            'matches': {'ones'},
        },
    ]

if __name__ == '__main__':
    unittest.main()

