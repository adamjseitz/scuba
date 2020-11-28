import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from grp import getgrgid
from pwd import getpwuid

from .config import find_config, ScubaConfig
from .config import ConfigError, ConfigNotFoundError
from .dockerutil import get_image_command
from .dockerutil import get_image_entrypoint
from .dockerutil import make_vol_opt
from .utils import shell_quote_cmd, flatten_list, StringIO


def verbose_msg(fmt, *args):
    # TODO: remove
    pass

class ScubaError(Exception):
    pass

class ScubaDive:
    def __init__(self, user_command, docker_args=None, env=None, as_root=False, verbose=False,
            image_override=None, entrypoint=None, shell_override=None, keep_tempfiles=False):

        env = env or {}
        if not isinstance(env, Mapping):
            raise ValueError('Argument env must be dict-like')

        self.as_root = as_root
        self.verbose = verbose
        self.entrypoint_override = entrypoint
        self.keep_tempfiles = keep_tempfiles

        # These will be added to docker run cmdline
        self.env_vars = env
        self.volumes = []
        self.options = docker_args or []
        self.workdir = None

        self.__locate_scubainit()

        # .scuba.yml is allowed to be missing if --image was given.
        self.__load_config(allow_missing=bool(image_override))

        # Process any aliases
        self.context = ScubaContext.process_command(
                                  cfg = self.config,
                                  command = user_command,
                                  image = image_override,
                                  shell = shell_override,
                                  )

        # Apply environment vars from .scuba.yml
        self.env_vars.update(self.context.environment)



    def prepare(self):
        '''Prepare to run the docker command'''
        self.__make_scubadir()

        if self.is_remote_docker:
            '''
            Docker is running remotely (e.g. boot2docker on OSX).
            We don't need to do any user setup whatsoever.

            TODO: For now, remote instances won't have any .scubainit

            See:
            https://github.com/JonathonReinhart/scuba/issues/17
            '''
            raise ScubaError('Remote docker not supported (DOCKER_HOST is set)')

        # Docker is running natively
        self.__setup_native_run()


    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        if not self.keep_tempfiles:
            shutil.rmtree(self.__scubadir_hostpath)

    def __str__(self):
        s = StringIO()
        s.writeln('ScubaDive')
        s.writeln('   verbose:      {}'.format(self.verbose))
        s.writeln('   as_root:      {}'.format(self.as_root))
        s.writeln('   workdir:      {}'.format(self.workdir))

        s.writeln('   options:')
        for a in self.options:
            s.writeln('      ' + a)

        s.writeln('   env_vars:')
        for k,v in self.env_vars.items():
            s.writeln('      {}={}'.format(k, v))

        s.writeln('   volumes:')
        for hostpath, contpath, options in self.__get_vol_opts():
            s.writeln('      {} => {} {}'.format(hostpath, contpath, options))

        s.writeln('   context:')
        s.writeln(str(self.context))

        return s.getvalue()




    @property
    def is_remote_docker(self):
        return 'DOCKER_HOST' in os.environ

    def add_env(self, name, val):
        '''Add an environment variable to the docker run invocation
        '''
        if name in self.env_vars:
            raise KeyError(name)
        self.env_vars[name] = val

    def add_volume(self, hostpath, contpath, options=None):
        '''Add a volume (bind-mount) to the docker run invocation
        '''
        if options is None:
            options = []
        self.volumes.append((hostpath, contpath, options))

    def add_option(self, option):
        '''Add another option to the docker run invocation
        '''
        self.options.append(option)

    def set_workdir(self, workdir):
        self.workdir = workdir

    def __locate_scubainit(self):
        '''Determine path to scubainit binary
        '''
        pkg_path = os.path.dirname(__file__)

        self.scubainit_path = os.path.join(pkg_path, 'scubainit')
        if not os.path.isfile(self.scubainit_path):
            raise ScubaError('scubainit not found at "{}"'.format(self.scubainit_path))

    def __load_config(self, allow_missing=False):
        '''Find and load .scuba.yml
        '''

        # top_path is where .scuba.yml is found, and becomes the top of our bind mount.
        # top_rel is the relative path from top_path to the current working directory,
        # and is where we'll set the working directory in the container (relative to
        # the bind mount point).
        try:
            top_path, top_rel, self.config = find_config()
        except ConfigNotFoundError as cfgerr:
            # If .scuba.yml is allowed to be missing, we assume a default config.
            if not allow_missing:
                raise ScubaError(str(cfgerr))
            top_path, top_rel = os.getcwd(), ''
            self.config = ScubaConfig(image=None)
        except ConfigError as cfgerr:
            raise ScubaError(str(cfgerr))

        # Mount scuba root directory at the same path in the container...
        self.add_volume(top_path, top_path)

        # ...and set the working dir relative to it
        self.set_workdir(os.path.join(top_path, top_rel))

        self.add_env('SCUBA_ROOT', top_path)

    def __make_scubadir(self):
        '''Make temp directory where all ancillary files are bind-mounted
        '''
        self.__scubadir_hostpath = tempfile.mkdtemp(prefix='scubadir')
        self.__scubadir_contpath = '/.scuba'
        self.add_volume(self.__scubadir_hostpath, self.__scubadir_contpath)

    def __setup_native_run(self):
        # These options are appended to mounted volume arguments
        # NOTE: This tells Docker to re-label the directory for compatibility
        # with SELinux. See `man docker-run` for more information.
        self.vol_opts = ['z']

        # Pass variables to scubainit
        self.add_env('SCUBAINIT_UMASK', '{:04o}'.format(get_umask()))

        # Check if the CLI args specify "run as root", or if the command (alias) does
        if not self.as_root and not self.context.as_root:
            uid = os.getuid()
            gid = os.getgid()
            self.add_env('SCUBAINIT_UID', uid)
            self.add_env('SCUBAINIT_GID', gid)
            self.add_env('SCUBAINIT_USER', getpwuid(uid).pw_name)
            self.add_env('SCUBAINIT_GROUP', getgrgid(gid).gr_name)

        if self.verbose:
            self.add_env('SCUBAINIT_VERBOSE', 1)


        # Copy scubainit into the container
        # We make a copy because Docker 1.13 gets pissed if we try to re-label
        # /usr, and Fedora 28 gives an AVC denial.
        scubainit_cpath = self.copy_scubadir_file('scubainit', self.scubainit_path)

        # Hooks
        for name in ('root', 'user', ):
            self.__generate_hook_script(name, self.context.shell)

        # allocate TTY if scuba's output is going to a terminal
        # and stdin is not redirected
        if sys.stdout.isatty() and sys.stdin.isatty():
            self.add_option('--tty')


        '''
        Normally, if the user provides no command to "docker run", the image's
        default CMD is run. Because we set the entrypiont, scuba must emulate the
        default behavior itself.
        '''
        if not self.context.script:
            # No user-provided command; we want to run the image's default command
            verbose_msg('No user command; getting command from image')
            default_cmd = get_image_command(self.context.image)
            if not default_cmd:
                raise ScubaError('No command given and no image-specified command')
            verbose_msg('{} Cmd: "{}"'.format(self.context.image, default_cmd))
            self.context.script = [shell_quote_cmd(default_cmd)]

        # Make scubainit the real entrypoint, and use the defined entrypoint as
        # the docker command (if it exists)
        self.add_option('--entrypoint={}'.format(scubainit_cpath))

        self.docker_cmd = []
        if self.entrypoint_override is not None:
            # --entrypoint takes precedence
            if self.entrypoint_override != '':
                self.docker_cmd = [self.entrypoint_override]
        elif self.context.entrypoint is not None:
            # then .scuba.yml
            if self.context.entrypoint != '':
                self.docker_cmd = [self.context.entrypoint]
        else:
            ep = get_image_entrypoint(self.context.image)
            if ep:
                self.docker_cmd = ep

        # The user command is executed via a generated shell script
        with self.open_scubadir_file('command.sh', 'wt') as f:
            self.docker_cmd += [self.context.shell, f.container_path]
            writeln(f, '# Auto-generated from scuba')
            writeln(f, 'set -e')
            for cmd in self.context.script:
                writeln(f, cmd)


    def open_scubadir_file(self, name, mode):
        '''Opens a file in the 'scubadir'

        This file will automatically be bind-mounted into the container,
        at a path given by the 'container_path' property on the returned file object.
        '''
        path = os.path.join(self.__scubadir_hostpath, name)
        assert not os.path.exists(path)

        # Make any directories required
        os.makedirs(os.path.dirname(path), exist_ok=True)

        f = open(path, mode)
        f.container_path = os.path.join(self.__scubadir_contpath, name)
        return f


    def copy_scubadir_file(self, name, source):
        '''Copies source into the scubadir

        Returns the container-path of the copied file
        '''
        dest = os.path.join(self.__scubadir_hostpath, name)
        assert not os.path.exists(dest)
        shutil.copy2(source, dest)

        return os.path.join(self.__scubadir_contpath, name)


    def __generate_hook_script(self, name, shell):
        script = self.config.hooks.get(name)
        if not script:
            return

        # Generate the hook script, mount it into the container, and tell scubainit
        with self.open_scubadir_file('hooks/{}.sh'.format(name), 'wt') as f:

            self.add_env('SCUBAINIT_HOOK_{}'.format(name.upper()), f.container_path)

            writeln(f, '#!{}'.format(shell))
            writeln(f, '# Auto-generated from .scuba.yml')
            writeln(f, 'set -e')
            for cmd in script:
                writeln(f, cmd)

    def __get_vol_opts(self):
        for hostpath, contpath, options in self.volumes:
            yield hostpath, contpath, options + self.vol_opts

    def get_docker_cmdline(self):
        args = ['docker', 'run',
            # interactive: keep STDIN open
            '-i',

            # remove container after exit
            '--rm',
        ]

        for name,val in self.env_vars.items():
            args.append('--env={}={}'.format(name, val))

        for hostpath, contpath, options in self.__get_vol_opts():
            args.append(make_vol_opt(hostpath, contpath, options))

        if self.workdir:
            args += ['-w', self.workdir]

        args += self.options

        # Docker image
        args.append(self.context.image)

        # Command to run in container
        args += self.docker_cmd

        return args


class ScubaContext:
    def __init__(self, image=None, script=None, entrypoint=None, environment=None, shell=None):
        self.image = image
        self.script = script
        self.as_root = False
        self.entrypoint = entrypoint
        self.environment = environment
        self.shell = shell

    def __str__(self):
        s = StringIO()
        s.writeln('ScubaContext')
        s.writeln('   image:        {}'.format(self.image))
        s.writeln('   script:       {}'.format(self.script))
        s.writeln('   as_root:      {}'.format(self.as_root))
        s.writeln('   entrypoint:   {}'.format(self.entrypoint))
        s.writeln('   environment:  {}'.format(self.environment))
        s.writeln('   shell:        {}'.format(self.shell))
        return s.getvalue()

    @classmethod
    def process_command(cls, cfg, command, image=None, shell=None):
        '''Processes a user command using aliases

        Arguments:
            config      ScubaConfig object
            command     A user command list (e.g. argv)
            image       Override the image from .scuba.yml
            shell       Override the shell from .scuba.yml

        Returns: A ScubaContext object with the following attributes:
            script: a list of command line strings
            image: the docker image name to use
        '''
        result = cls(
                entrypoint = cfg.entrypoint,
                environment = cfg.environment.copy(),
                shell = cfg.shell,
                )

        if command:
            alias = cfg.aliases.get(command[0])
            if not alias:
                # Command is not an alias; use it as-is.
                result.script = [shell_quote_cmd(command)]
            else:
                # Using an alias
                # Does this alias override the image and/or entrypoint?
                if alias.image:
                    result.image = alias.image
                if alias.entrypoint is not None:
                    result.entrypoint = alias.entrypoint
                if alias.shell is not None:
                    result.shell = alias.shell
                if alias.as_root:
                    result.as_root = True

                # Merge/override the environment
                if alias.environment:
                    result.environment.update(alias.environment)

                if len(alias.script) > 1:
                    # Alias is a multiline script; no additional
                    # arguments are allowed in the scuba invocation.
                    if len(command) > 1:
                        raise ConfigError('Additional arguments not allowed with multi-line aliases')
                    result.script = alias.script

                else:
                    # Alias is a single-line script; perform substituion
                    # and add user arguments.
                    command.pop(0)
                    result.script = [alias.script[0] + ' ' + shell_quote_cmd(command)]

            result.script = flatten_list(result.script)

        # If a shell was given on the CLI, it should override the shell set by
        # the alias or top-level config
        if shell:
            result.shell = shell

        # If an image was given, it overrides what might have been set by an alias
        if image:
            result.image = image

        # If the image was still not set, then try to get it from the confg,
        # which will raise a ConfigError if it is not set
        if not result.image:
            result.image = cfg.image

        return result


def writeln(f, line):
    f.write(line + '\n')


def get_umask():
    # Same logic as bash/builtins/umask.def
    val = os.umask(0o22)
    os.umask(val)
    return val
