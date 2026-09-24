
import codecs
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import ClassVar

if importlib.util.find_spec('winreg'):
    import winreg
from xml.dom import minidom
from xml.parsers.expat import ExpatError

from IPython.display import SVG, Image
from metakernel import MetaKernel, ProcessMetaKernel, REPLWrapper, pexpect
from metakernel.pexpect import which

from . import __version__

# Scilab's terminal control codes (mode/cursor resets, backspace) end up
# glued to the front of the *first* line of any console output -- used to
# strip those out of completion results, see get_completions() below.
_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\x08')


class _ScilabREPLWrapper(REPLWrapper):
    """A :class:`REPLWrapper` that knows how to escape Scilab's continuation
    prompt.

    When a cell leaves a block unclosed (``function x=f(y)`` with no
    ``endfunction``, an ``if``/``for``/``while``/... with no matching
    ``end``, ...), Scilab drops into a continuation prompt waiting for the
    rest of the block. REPLWrapper's own recovery for this sends Ctrl-C and
    waits (up to 30s) for a normal prompt to come back -- but
    ``scilab-adv-cli`` does not respond to SIGINT while waiting for more
    input, so that 30s is always spent in full, and the user just sees a
    generic "Timed out" error after a long pause.

    A bare "end" closes any Scilab block type and returns to the top-level
    prompt in well under a second; nested unclosed blocks need one "end"
    per level, so it is sent repeatedly until a normal prompt reappears.
    """

    _MAX_END_ATTEMPTS = 50

    def interrupt(self, continuation=False):
        if not continuation:
            return super().interrupt(continuation=continuation)
        for _ in range(self._MAX_END_ATTEMPTS):
            self.sendline("end")
            if self._expect_prompt(timeout=-1) == 0:
                break
        return self.child.before


def get_kernel_json():
    """Get the kernel json for the kernel.
    """
    here = os.path.dirname(__file__)
    with open(os.path.join(here, 'kernel.json')) as fid:
        data = json.load(fid)
    data['argv'][0] = sys.executable
    return data


class ScilabKernel(ProcessMetaKernel):
    app_name = 'scilab_kernel'
    implementation = 'Scilab Kernel'
    implementation_version = __version__,
    language = 'scilab'
    language_version = __version__,
    language_info: ClassVar[dict] = {
        'name': 'scilab',
        'file_extension': '.sci',
        # JupyterLab's CodeMirror editor has no built-in "scilab" language
        # mode, so real syntax highlighting (correct "//" comments,
        # keywords, etc.) needs the companion "jupyterlab-scilab" extension
        # (https://pypi.org/project/jupyterlab-scilab/, `pip install
        # scilab-kernel[jupyterlab]`), which registers one. Without it,
        # cells show as plain text in the live editor -- nbconvert/Pygments
        # export is unaffected either way, since that path uses the 'name'
        # above, which is a real Pygments lexer ("scilab").
        #
        # codemirror_mode is set explicitly (rather than relying on
        # JupyterLab falling back to matching language_info.name against
        # its registry) because that fallback goes through a codepath with
        # a key-name mismatch bug in JupyterLab 4.6's
        # getMimeTypeByLanguage() for the *mimetype* half of the fallback
        # (mimetype vs mime, ext vs extensions) -- harmless here only
        # because the *name* half happens to match first, but not
        # something to depend on implicitly.
        "mimetype": "text/x-scilab",
        "codemirror_mode": "scilab",
        "version": __version__,
        'help_links': MetaKernel.help_links,
    }
    kernel_json = get_kernel_json()

    _setup = (
        f'lines(0, 800); // TODO: Scilab kernel does not detect output width\n'
        f'mode(0);\n'
        f'try,getd("."),end\n'
        f'try,getd("{os.path.dirname(__file__)}"),end\n'
    )

    _first = True

    _banner = None

    _executable = None
    _default_args = None

    @property
    def executable(self):
        if self._executable:
            return self._executable
        
        executable = next(self._detect_executable())
        if not executable:
            msg = ('Scilab Executable not found, please add to path or set '
                    '"SCILAB_EXECUTABLE" environment variable')
            raise OSError(msg)
        
        if 'cli' not in executable:
            self._default_args = ['-nw']
        else:
            self._default_args = []
        self.log.debug(' scilab_kernel._executable: ' + executable)
        self.log.debug(' scilab_kernel._default_args: ' + ' '.join(self._default_args))
        self._executable = executable
        return executable

    @property
    def banner(self):
        if self._banner is None:
            resp = self.do_execute_direct("getversion()", silent=True)
            if resp:
                #  ans  =
                #
                #  "scilab-branch-2024.1"
                result = re.search(r'scilab-([a-zA-Z0-9\-\.]+)', resp.output)
                if result:
                    self._banner = result.group(1)
                    self.log.warning(' scilab_kernel._banner: ' + self._banner)
        if self._banner is None:
            self._banner = "Unknown version"
        return self._banner

    def _detect_executable(self):
        # the env. variable can be setup
        executable = os.environ.get('SCILAB_EXECUTABLE', None)
        if executable:
            self.log.warning('SCILAB_EXECUTABLE env. variable: ' + executable)
            yield executable

        # read the windows registry
        if os.name == 'nt':
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Scilab5.sce\shell\open\command") as key:
                    cmd : str = winreg.EnumValue(key, 0)[1]
                    executable = cmd.split(r'"')[1].replace("wscilex.exe", "wscilex-cli.exe")
                    self.log.warning('Windows registry binary: ' + executable)
                    yield executable
            except FileNotFoundError:
                pass

        # detect macOS bundle
        if platform.system() == 'Darwin':
            process = subprocess.run(['mdfind', '-onlyin', '/Applications', 'kMDItemCFBundleIdentifier=org.scilab.modules.jvm.Scilab'],
                                 stdout=subprocess.PIPE,
                                 text=True, check=False)
            bundles = process.stdout
            if len(bundles) > 0:
                executable = bundles.split('\n', 1)[0] + "/Contents/bin/scilab-adv-cli"
                self.log.warning('macOS Application binary: ' + executable)
                yield executable

        # detect on the path
        if os.name == 'nt':
            executable = 'WScilex-cli'
        else:
            executable = 'scilab-adv-cli'
        executable = which(executable)
        if executable:
            self.log.warning('executable in the path: ' + executable)
            yield executable

        # default to None, will report an error
        yield None

    def makeWrapper(self):
        """Start a Scilab process and return a :class:`REPLWrapper` object.
        """

        orig_prompt = r'-[0-9]*->'
        prompt_cmd = None
        change_prompt = None
        continuation_prompt = r'  \>'
        self._first = True
        if os.name == 'nt':
            prompt_cmd = 'printf("-->")'
            echo = False
        else:
            echo = True
        executable = self.executable
        child = pexpect.spawnu(executable, self._default_args,
            echo=echo,
            codec_errors="ignore",
            encoding="utf-8")
        wrapper = _ScilabREPLWrapper(child, orig_prompt, change_prompt,
            prompt_emit_cmd=prompt_cmd, echo=echo,
            continuation_prompt_regex=continuation_prompt)
        
        wrapper.child.linesep = '\r\n' if os.name == 'nt' else '\n'
        return wrapper
    
    def Write(self, message):
        clean_msg = message.strip("\n\r\t")
        super().Write(clean_msg)

    def Print(self, text):
        text = str(text).strip('\x1b[0m').replace('\u0008', '').strip()
        text = [line.strip() for line in text.splitlines()
                if (not line.startswith(chr(27)))]
        text = '\n'.join(text)
        if text:
            super().Print(text)

    def do_execute_direct(self, code, silent=False):
        if self._first:
            self._first = False
            self.handle_plot_settings()
            setup = self._setup.strip()
            self.do_execute_direct(setup, True)
        resp = super().do_execute_direct(code, silent=silent)
        if silent:
            return resp
        if self.plot_settings.get('backend', None) == 'inline':
            plot_dir = self.make_figures()
            for image in self.extract_figures(plot_dir):
                self.Display(image)
            shutil.rmtree(plot_dir, True)

    def get_kernel_help_on(self, info, level=0, none_on_fail=False):
        obj = info.get('help_obj', '')
        if not obj or len(obj.split()) > 1:
            if none_on_fail:
                return None
            else:
                return ""
        self.do_execute_direct(f'help {obj}', True)

    def do_shutdown(self, restart):
        self.wrapper.sendline('quit')
        super().do_shutdown(restart)

    def get_completions(self, info):
        """
        Get completions from kernel based on info dict.
        """
        obj = info['obj']
        # metakernel's do_complete() calls get_completions() even when the
        # cursor sits right after a non-identifier character (e.g. "+",
        # "-", "/") and there is no partial word to complete -- info['obj']
        # is "" in that case. completion("") then matches every name
        # Scilab knows, which is intentional: it's the same convention
        # IPython follows (Tab on an empty prefix browses everything), and
        # scilab-adv-cli's own interactive completion does the same thing.
        #
        # completion() displays its result on Scilab's own console, quoted
        # (Scilab now shows string arrays as `"a"  "b"  ...`), which broke
        # the parsing below; printf("%s\n", ...) instead prints each match
        # bare, one per line. Only printf when there is at least one match:
        # it errors out on an empty array. Both calls run in the same
        # persistent Scilab session as the user's own code, so nothing here
        # is assigned to a variable that could shadow one of theirs.
        cmd = (
            f'if ~isempty(completion("{obj}")) then '
            f'printf("%s\\n",completion("{obj}")); end'
        )
        output = self.do_execute_direct(cmd, True)
        if not output:
            return []
        text = _ANSI_ESCAPE_RE.sub('', output.output)
        return [line.strip() for line in text.splitlines()
                if info['obj'] in line]

    async def do_complete(self, code, cursor_pos):
        content = await super().do_complete(code, cursor_pos)
        if not content.get('matches'):
            # metakernel's parser sets cursor_start to 0 (start of buffer)
            # whenever there is no partial word at the cursor, regardless
            # of whether any completions were actually found -- e.g. after
            # "plot(1:10,", cursor_start ends up 0 with cursor_end at the
            # cursor position, even though there is nothing to complete.
            # With zero matches that wide range should never matter to a
            # kernel-only completer, but JupyterLab also has its own
            # generic word-from-document completer active alongside the
            # kernel one, and a wide, kernel-reported range spanning real
            # words ("plot", "1", "10") gives it something to offer -- an
            # unwanted menu of words already on the line. Collapsing to a
            # zero-width point at the cursor whenever we have nothing
            # avoids handing it that range.
            content['cursor_start'] = content['cursor_end']
        return content

    def handle_plot_settings(self):
        """Handle the current plot settings"""
        settings = self.plot_settings
        settings.setdefault('backend', 'inline')
        settings.setdefault('format', 'svg')
        settings.setdefault('size', '560,420')
        settings.setdefault('antialiasing', True)
        
        cmds = []

        self._plot_fmt = settings['format']

        cmds.append('h = gdf();')
        cmds.append('h.figure_position = [0, 0];')

        width, height = 560, 420
        if isinstance(settings['size'], tuple):
            width, height = settings['size']
        elif settings['size']:
            try:
                width, height = settings['size'].split(',')
                width, height = int(width), int(height)
            except (ValueError, AttributeError) as e:
                self.Error(f'Error setting plot settings: {e}')

        cmds.append(f'h.figure_size = [{width},{height}];')
        cmds.append(f'h.axes_size = [{width} * 0.98, {height} * 0.8];')

        if settings['backend'] == 'inline':
            cmds.append('h.visible = "off";')
        else:
            cmds.append('h.visible = "on";')
        super().do_execute_direct('\n'.join(cmds), True)

    def make_figures(self, plot_dir=None):
        """Create figures for the current figures.

        Parameters
        ----------
        plot_dir: str, optional
            The directory in which to create the plots.

        Returns
        -------
        out: str
            The plot directory containing the files.
        """
        plot_dir = plot_dir or tempfile.mkdtemp()
        plot_format = self._plot_fmt.lower()
        make_figs = '_make_figures("%s", "%s");'
        make_figs = make_figs % (plot_dir, plot_format)
        super().do_execute_direct(make_figs, True)
        return plot_dir

    def extract_figures(self, plot_dir):
        """Get a list of IPython Image objects for the created figures.

        Parameters
        ----------
        plot_dir: str
            The directory in which to create the plots.
        """
        images = []
        for fname in sorted(os.listdir(plot_dir)):
            filename = os.path.join(plot_dir, fname)
            try:
                if fname.lower().endswith('.svg'):
                    im = self._handle_svg(filename)
                else:
                    im = Image(filename)
                images.append(im)
            except Exception as e:
                if self.error_handler:
                    self.error_handler(e)
                else:
                    raise
        return images

    def _handle_svg(self, filename):
        """
        Handle special considerations for SVG images.
        """
        # Gnuplot can create invalid characters in SVG files.
        with codecs.open(filename, 'r', encoding='utf-8',
                         errors='replace') as fid:
            data = fid.read()
        im = SVG(data=data)
        try:
            im.data = self._fix_svg_size(im.data)
        except (ValueError, ExpatError) as e:
            self.log.debug(f'Could not resize SVG (unexpected shape from GnuPlot?): {e}')
        try:
            settings = self.plot_settings
            if settings['antialiasing']:
                im.data = self._fix_svg_antialiasing(im.data)
        except (ValueError, ExpatError) as e:
            self.log.debug(f'Could not adjust SVG antialiasing (unexpected shape from GnuPlot?): {e}')
        return im

    def _fix_svg_size(self, data):
        """GnuPlot SVGs do not have height/width attributes.  Set
        these to be the same as the viewBox, so that the browser
        scales the image correctly.
        """
        # Minidom does not support parseUnicode, so it must be decoded
        # to accept unicode characters
        parsed = minidom.parseString(data.encode('utf-8'))
        (svg,) = parsed.getElementsByTagName('svg')

        viewbox = svg.getAttribute('viewBox').split(' ')
        width, height = viewbox[2:]
        width, height = int(width), int(height)

        # Handle overrides in case they were not encoded.
        settings = self.plot_settings
        if settings['width'] != -1:
            if settings['height'] == -1:
                height = height * settings['width'] / width
            width = settings['width']
        if settings['height'] != -1:
            if settings['width'] == -1:
                width = width * settings['height'] / height
            height = settings['height']

        svg.setAttribute('width', f'{int(width)}px')
        svg.setAttribute('height', f'{int(height)}px')
        return svg.toxml()

    def _fix_svg_antialiasing(self, data):
        """Batik API to change line art antialias is broken.
        We add shape-rendering:geometricPrecision to content with style containing "clip-path:url(#clipPath1)"
        """
        # Minidom does not support parseUnicode, so it must be decoded
        # to accept unicode characters
        parsed = minidom.parseString(data.encode('utf-8'))
        (svg,) = parsed.getElementsByTagName('svg')
        g = svg.getElementsByTagName('path')
        for i in range(len(g)):
            stylestr = g[i].getAttribute('style').replace("clip-path:url(#clipPath", "shape-rendering:geometricPrecision; clip-path:url(#clipPath")
            g[i].setAttribute('style', stylestr)
        return svg.toxml()

