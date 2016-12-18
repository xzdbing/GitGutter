import os
import subprocess
import re
import codecs
import tempfile
import time
from functools import partial

import sublime

try:
    from .git_gutter_settings import settings
    from .promise import Promise
except (ImportError, ValueError):
    from git_gutter_settings import settings
    from promise import Promise

try:
    from subprocess import TimeoutExpired
    _HAVE_TIMEOUT = True
except:
    class TimeoutExpired(Exception):
        pass
    _HAVE_TIMEOUT = False


class GitGutterHandler(object):
    # the git repo won't change that often so we can easily wait few seconds
    # between updates for performance
    git_file_update_interval_secs = 5

    def __init__(self, view):
        self.view = view

        self.git_temp_file = None
        self.buf_temp_file = None

        # cached view file name to detect renames
        self._view_file_name = None
        # real path to current work tree
        self._git_tree = None
        # relative file path in work tree
        self._git_path = None
        self.git_tracked = False

        self._last_refresh_time_git_file = 0

    def __del__(self):
        """Delete temporary files."""
        if self.git_temp_file:
            os.unlink(self.git_temp_file)
        if self.buf_temp_file:
            os.unlink(self.buf_temp_file)

    @staticmethod
    def tmp_file():
        """Create a temp file and return the filepath to it.

        CAUTION: Caller is responsible for clean up
        """
        fd, filepath = tempfile.mkstemp(prefix='git_gutter_')
        os.close(fd)
        return filepath

    @property
    def repository_name(self):
        """Return the folder name of the working tree as repository name."""
        return os.path.basename(
            self._git_tree) if self._git_tree else '(None)'

    def work_tree(self, validate=False):
        """Return the real path of a valid work-tree or None.

        Arguments:
            validate (bool): If True check whether the file is part of a valid
                             git repository or return the cached working tree
                             path only on False.
        """
        def is_work_tree(path):
            """Return True if `path` contains a `.git` directory or file."""
            return path and os.path.exists(os.path.join(path, '.git'))

        def split_work_tree(file_path):
            """Split the `file_path` into working tree and relative path.

            The file_path is converted to a absolute real path and split into
            the working tree part and relative path part.

            Note:
                This is a local alternitive to calling the git command:

                    git rev-parse --show-toplevel

            Arguments:
                file_path (string): full path to a file.

            Returns:
                A tuble of two the elements (working tree, file path).
            """
            if file_path:
                path, name = os.path.split(os.path.realpath(file_path))
                # files within '.git' path are not part of a work tree
                while path and name and name != '.git':
                    if is_work_tree(path):
                        return (path, os.path.relpath(
                            file_path, path).replace('\\', '/'))
                    path, name = os.path.split(path)
            return (None, None)

        if validate:
            # Check if file exists
            file_name = self.view.file_name()
            if not file_name or not os.path.isfile(file_name):
                self._view_file_name = None
                self._git_tree = None
                self._git_path = None
                return None
            # Check if file was renamed
            is_renamed = file_name != self._view_file_name
            if is_renamed or not is_work_tree(self._git_tree):
                self._view_file_name = file_name
                self._git_tree, self._git_path = split_work_tree(file_name)
                self.clear_git_time()
        return self._git_tree

    def git_time_cleared(self):
        return self._last_refresh_time_git_file == 0

    def clear_git_time(self):
        self._last_refresh_time_git_file = 0

    def update_git_time(self):
        self._last_refresh_time_git_file = time.time()

    def git_time(self):
        return time.time() - self._last_refresh_time_git_file

    def get_compare_against(self):
        """Return the branch/commit/tag string the view is compared to."""
        return settings.get_compare_against(self._git_tree, self.view)

    def set_compare_against(self, commit, refresh=False):
        """Apply a new branch/commit/tag string the view is compared to.

        If one of the settings 'focus_change_mode' or 'live_mode' is true,
        the view, is automatically compared by 'on_activate' event when
        returning from a quick panel and therefore the command 'git_gutter'
        can be ommited. This assumption can be overriden by 'refresh' for
        commands that do not show a quick panel.

        Arguments:
            commit  - is either a branch, commit or tag as returned from
                      git show-ref
            refresh - always call git_gutter command
        """
        settings.set_compare_against(self._git_tree, commit)
        self.clear_git_time()
        if refresh or not any(settings.get(key, True) for key in (
                'focus_change_mode', 'live_mode')):
            self.view.run_command('git_gutter')  # refresh ui

    def format_compare_against(self):
        """Format the compare against setting to use for display."""
        comparing = self.get_compare_against()
        for repl in ('refs/heads/', 'refs/remotes/', 'refs/tags/'):
            comparing = comparing.replace(repl, '')
        return comparing

    def _get_view_encoding(self):
        """Read view encoding and transform it for use with python.

        This method reads `origin_encoding` used by ConvertToUTF8 plugin and
        goes on with ST's encoding setting if required. The encoding is
        transformed to work with python's `codecs` module.

        Returns:
            string: python compatible view encoding
        """
        encoding = self.view.settings().get('origin_encoding')
        if not encoding:
            encoding = self.view.encoding()
            if encoding == "Undefined":
                encoding = self.view.settings().get('default_encoding')
            begin = encoding.find('(')
            if begin > -1:
                encoding = encoding[begin + 1:-1]
            encoding = encoding.replace('with BOM', '')
            encoding = encoding.replace('Windows', 'cp')
            encoding = encoding.replace('-', '_')
        encoding = encoding.replace(' ', '')
        return encoding

    def in_repo(self):
        """Return true, if the most recent `git show` returned any content.

        If `git show` returns empty content, any diff will result in
        all lines added state and the view's file is most commonly untracked.
        """
        return self.git_tracked

    def update_buf_file(self):
        """Write view's content to temporary file as source for git diff."""
        # Read from view buffer
        chars = self.view.size()
        region = sublime.Region(0, chars)
        contents = self.view.substr(region)
        # Try conversion
        try:
            encoding = self._get_view_encoding()
            encoded = contents.encode(encoding)
        except (LookupError, UnicodeError):
            # Fallback to utf8-encoding
            encoded = contents.encode('utf-8')
        # Write the encoded content to file
        if not self.buf_temp_file:
            self.buf_temp_file = GitGutterHandler.tmp_file()
        with open(self.buf_temp_file, 'wb') as f:
            if self.view.encoding() == "UTF-8 with BOM":
                f.write(codecs.BOM_UTF8)
            f.write(encoded)

    def update_git_file(self):

        def write_file(contents):
            contents = contents.replace(b'\r\n', b'\n')
            contents = contents.replace(b'\r', b'\n')
            self.git_tracked = bool(contents)
            with open(self.git_temp_file, 'wb') as f:
                f.write(contents)

        if self.git_time() > self.git_file_update_interval_secs:
            if not self.git_temp_file:
                self.git_temp_file = GitGutterHandler.tmp_file()

            args = [
                settings.git_binary_path,
                'show',
                '%s:%s' % (
                    self.get_compare_against(),
                    self._git_path),
            ]
            return self.run_command(args=args, decode=False).then(write_file)
        return Promise.resolve()

    def process_diff(self, diff_str):
        r"""Parse unified diff with 0 lines of context.

        Hunk range info format:
          @@ -3,2 +4,0 @@
            Hunk originally starting at line 3, and occupying 2 lines, now
            starts at line 4, and occupies 0 lines, i.e. it was deleted.
          @@ -9 +10,2 @@
            Hunk size can be omitted, and defaults to one line.
        Dealing with ambiguous hunks:
          "A\nB\n" -> "C\n"
          Was 'A' modified, and 'B' deleted? Or 'B' modified, 'A' deleted?
          Or both deleted? To minimize confusion, let's simply mark the
          hunk as modified.
        """
        inserted = []
        modified = []
        deleted = []
        hunk_re = '^@@ \-(\d+),?(\d*) \+(\d+),?(\d*) @@'
        hunks = re.finditer(hunk_re, diff_str, re.MULTILINE)
        for hunk in hunks:
            start = int(hunk.group(3))
            old_size = int(hunk.group(2) or 1)
            new_size = int(hunk.group(4) or 1)
            if not old_size:
                inserted += range(start, start + new_size)
            elif not new_size:
                deleted += [start + 1]
            else:
                modified += range(start, start + new_size)
        return (inserted, modified, deleted)

    def diff_str(self):
        """Run git diff against view and decode the result then."""

        def decode_diff(results):
            encoding = self._get_view_encoding()
            try:
                decoded_results = results.decode(encoding)
            except UnicodeError:
                try:
                    decoded_results = results.decode("utf-8")
                except UnicodeDecodeError:
                    decoded_results = ""
            except LookupError:
                try:
                    decoded_results = codecs.decode(results)
                except UnicodeDecodeError:
                    decoded_results = ""
            return decoded_results

        def run_diff(_unused):
            self.update_buf_file()
            args = [
                settings.git_binary_path,
                'diff', '-U0', '--no-color', '--no-index',
                settings.ignore_whitespace,
                settings.patience_switch,
                self.git_temp_file,
                self.buf_temp_file,
            ]
            args = list(filter(None, args))  # Remove empty args
            return self.run_command(args=args, decode=False).then(decode_diff)
        return self.update_git_file().then(run_diff)

    def process_diff_line_change(self, line_nr, diff_str):
        hunk_re = '^@@ \-(\d+),?(\d*) \+(\d+),?(\d*) @@'
        hunks = re.finditer(hunk_re, diff_str, re.MULTILINE)

        # we also want to extract the position of the surrounding changes
        first_change = prev_change = next_change = None

        for hunk in hunks:
            start = int(hunk.group(3))
            size = int(hunk.group(4) or 1)
            if first_change is None:
                first_change = start
            # special handling to also match the line below deleted
            # content
            if size == 0 and line_nr == start + 1:
                pass
            # continue if the hunk is before the line
            elif start + size < line_nr:
                prev_change = start
                continue
            # break if the hunk is after the line
            elif line_nr < start:
                break
            # in the following the line is inside the hunk
            try:
                next_hunk = next(hunks)
                hunk_end = next_hunk.start()
                next_change = int(next_hunk.group(3))
            except:
                hunk_end = len(diff_str)

            # if wrap is disable avoid wrapping
            wrap = settings.get('next_prev_change_wrap', True)
            if not wrap:
                if prev_change is None:
                    prev_change = start
                if next_change is None:
                    next_change = start

            # if prev change is None set it to the wrap around the
            # document: prev -> last hunk, next -> first hunk
            if prev_change is None:
                try:
                    remaining_hunks = list(hunks)
                    if remaining_hunks:
                        last_hunk = remaining_hunks[-1]
                        prev_change = int(last_hunk.group(3))
                    elif next_change is not None:
                        prev_change = next_change
                    else:
                        prev_change = start
                except:
                    prev_change = start
            if next_change is None:
                next_change = first_change

            # extract the content of the hunk
            hunk_content = diff_str[hunk.start():hunk_end]
            # store all deleted lines (starting with -)
            hunk_lines = hunk_content.splitlines()[1:]
            deleted_lines = [
                line[1:] for line in hunk_lines if line.startswith("-")
            ]
            added_lines = [line[1:] for line in hunk_lines
                           if line.startswith("+")]
            meta = {
                "added_lines": added_lines,
                "first_change": first_change,
                "next_change": next_change,
                "prev_change": prev_change
            }
            return (deleted_lines, start, size, meta)
        return ([], -1, -1, {})

    def diff_line_change(self, line):
        """Run git diff and extract the changes of a certain line.

        NOTE: This method is used for diff popup.
        """
        return self.diff_str().then(
            partial(self.process_diff_line_change, line))

    def diff(self):
        """Run git diff to check for inserted, modified and deleted lines.

        NOTE: This method is used to update the gutter markers.
        """
        return self.diff_str().then(self.process_diff)

    def untracked(self):
        """Determine whether the view shows an untracked file."""
        return self.handle_files([])

    def ignored(self):
        """Determine whether the view shows an ignored file."""
        return self.handle_files(['-i'])

    def handle_files(self, additional_args):
        """Run git ls-files to check for untracked or ignored file."""
        if self._git_tree:
            def is_nonempty(results):
                """Determine if view's file is in git's index.

                If the view's file is not part of the index
                git returns empty output to stdout.
                """
                return bool(results)

            args = [
                settings.git_binary_path,
                'ls-files', '--other', '--exclude-standard',
            ] + additional_args + [
                os.path.join(self._git_tree, self._git_path),
            ]
            args = list(filter(None, args))  # Remove empty args
            return self.run_command(args).then(is_nonempty)
        return Promise.resolve(False)

    def git_commits(self):
        r"""Query all commits.

        The git output will have following format splitted by \a:
            <hash> <title>
            <name> <email>
            <date> (<time> ago)
        """
        args = [
            settings.git_binary_path,
            'log', '--all',
            '--pretty=%h %s\a%an <%aE>\a%ad (%ar)',
            '--date=local', '--max-count=9000'
        ]
        return self.run_command(args)

    def git_file_commits(self):
        r"""Query all commits with changes to the attached file.

        The git output will have following format splitted by \a:
            <timestamp>
            <hash> <title>
            <name> <email>
            <date> (<time> ago)
        """
        args = [
            settings.git_binary_path,
            'log',
            '--pretty=%at\a%h %s\a%an <%aE>\a%ad (%ar)',
            '--date=local', '--max-count=9000',
            '--', self._git_path
        ]
        return self.run_command(args)

    def git_branches(self):
        args = [
            settings.git_binary_path,
            'for-each-ref',
            '--sort=-committerdate',
            '--format=%(subject)\a%(refname)\a%(objectname)',
            'refs/heads/'
        ]
        return self.run_command(args)

    def git_tags(self):
        args = [
            settings.git_binary_path,
            'show-ref',
            '--tags',
            '--abbrev=7'
        ]
        return self.run_command(args)

    def git_current_branch(self):
        args = [
            settings.git_binary_path,
            'rev-parse',
            '--abbrev-ref',
            'HEAD'
        ]
        return self.run_command(args)

    def run_command(self, args, decode=True):
        """Run a git command asynchronously and return a Promise.

        Arguments:
            args    - a list of arguments used to create the git subprocess.
            decode  - if True the git's output is decoded assuming utf-8
                      which is the default output encoding of git.
        """
        def read_output(resolve):
            """Start git process and forward its output to the Resolver."""
            try:
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                else:
                    startupinfo = None
                proc = subprocess.Popen(
                    args=args, cwd=self._git_tree, startupinfo=startupinfo,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE)
                if _HAVE_TIMEOUT:
                    stdout, stderr = proc.communicate(timeout=30)
                else:
                    stdout, stderr = proc.communicate()
            except OSError as error:
                print('GitGutter failed to run git: %s' % error)
                stdout = b''
            except TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            finally:
                if decode:
                    resolve(stdout.decode('utf-8').strip())
                else:
                    resolve(stdout)

        def run_async(resolve):
            if hasattr(sublime, 'set_timeout_async'):
                sublime.set_timeout_async(lambda: read_output(resolve), 0)
            else:
                read_output(resolve)

        return Promise(run_async)
