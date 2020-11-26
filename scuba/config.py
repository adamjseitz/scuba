import os
import yaml
import re
import shlex

from .constants import *
from .utils import *

class ConfigError(Exception):
    pass

class ConfigNotFoundError(ConfigError):
    pass

# http://stackoverflow.com/a/9577670
class Loader(yaml.SafeLoader):
    def __init__(self, stream):
        self._root = os.path.split(stream.name)[0]
        self._cache = dict()
        super().__init__(stream)

    def from_yaml(self, node):
        '''
        Implementes a !from_yaml constructor with the following syntax:
            !from_yaml filename key

        Arguments:
            filename:   Filename of external YAML document from which to load,
                        relative to the current YAML file.
            key:        Key from external YAML document to return,
                        using a dot-separated syntax for nested keys.

        Examples:
            !from_yaml external.yml pop
            !from_yaml external.yml foo.bar.pop
            !from_yaml "another file.yml" "foo bar.snap crackle.pop"
        '''

        # Load the content from the node, as a scalar
        content = self.construct_scalar(node)

        # Split on unquoted spaces
        parts = shlex.split(content)
        if len(parts) != 2:
            raise yaml.YAMLError('Two arguments expected to !from_yaml')
        filename, key = parts

        # path is relative to the current YAML document
        path = os.path.join(self._root, filename)

        # Load the other YAML document
        doc = self._cache.get(path)
        if not doc:
            with open(path, 'r') as f:
                doc = yaml.load(f, self.__class__)
                self._cache[path] = doc

        # Retrieve the key
        try:
            cur = doc
            # Use a negative look-behind to split the key on non-escaped '.' characters
            for k in re.split(r'(?<!\\)\.', key):
                cur = cur[k.replace('\\.', '.')]  # Be sure to replace any escaped '.' characters with *just* the '.'
        except KeyError:
            raise yaml.YAMLError('Key "{}" not found in {}'.format(key, filename))
        return cur

Loader.add_constructor('!from_yaml', Loader.from_yaml)


def find_config():
    '''Search up the directory hierarchy for .scuba.yml

    Returns: path, rel, config on success, or None if not found
        path    The absolute path of the directory where .scuba.yml was found
        rel     The relative path from the directory where .scuba.yml was found
                to the current directory
        config  The loaded configuration
    '''
    cross_fs = 'SCUBA_DISCOVERY_ACROSS_FILESYSTEM' in os.environ
    path = os.getcwd()

    rel = ''
    while True:
        cfg_path = os.path.join(path, SCUBA_YML)
        if os.path.exists(cfg_path):
            return path, rel, load_config(cfg_path)

        if not cross_fs and os.path.ismount(path):
            msg = '{} not found here or any parent up to mount point {}'.format(SCUBA_YML, path) \
                   + '\nStopping at filesystem boundary (SCUBA_DISCOVERY_ACROSS_FILESYSTEM not set).'
            raise ConfigNotFoundError(msg)

        # Traverse up directory hierarchy
        path, rest = os.path.split(path)
        if not rest:
            raise ConfigNotFoundError('{} not found here or any parent directories'.format(SCUBA_YML))

        # Accumulate the relative path back to where we started
        rel = os.path.join(rest, rel)


def _process_script_node(node, name):
    '''Process a script-type node

    This handles nodes that follow the *Common script schema*,
    as outlined in doc/yaml-reference.md.
    '''
    if isinstance(node, str):
        # The script is just the text itself
        return [node]


    if isinstance(node, dict):
        # There must be a "script" key, which must be a list of strings
        script = node.get('script')
        if not script:
            raise ConfigError("{}: must have a 'script' subkey".format(name))

        if isinstance(script, list):
            return script

        if isinstance(script, str):
            return [script]

        raise ConfigError("{}.script: must be a string or list".format(name))

    raise ConfigError("{}: must be string or dict".format(name))


def _process_environment(node, name):
    # Environment can be either a list of strings ("KEY=VALUE") or a mapping
    # Environment keys and values are always strings
    result = {}

    if not node:
        pass
    elif isinstance(node, dict):
        for k, v in node.items():
            if v is None:
                v = os.getenv(k, '')
            result[k] = str(v)
    elif isinstance(node, list):
        for e in node:
            k, v = parse_env_var(e)
            result[k] = v
    else:
        raise ConfigError("'{}' must be list or mapping, not {}".format(
                name, type(node).__name__))

    return result

def _get_entrypoint(data):
    # N.B. We can't use data.get() here, because that might return
    # None, leading to ambiguity between entrypoint being absent or set
    # to a null value.
    #
    # "Note that a null is different from an empty string and that a
    # mapping entry with some key and a null value is valid and
    # different from not having that key in the mapping."
    #   - http://yaml.org/type/null.html
    key = 'entrypoint'

    if not key in data:
        return None

    ep = data[key]

    # We represent a null value as an empty string.
    if ep is None:
        ep = ''

    if not isinstance(ep, str):
        raise ConfigError("'{}' must be a string, not {}".format(
                key, type(ep).__name__))
    return ep


class ScubaAlias:
    def __init__(self, name, script, image, entrypoint, environment, shell, as_root):
        self.name = name
        self.script = script
        self.image = image
        self.entrypoint = entrypoint
        self.environment = environment
        self.shell = shell
        self.as_root = as_root

    @classmethod
    def from_dict(cls, name, node):
        script = _process_script_node(node, name)
        image = None
        entrypoint = None
        environment = None
        shell = None
        as_root = False

        if isinstance(node, dict):  # Rich alias
            image = node.get('image')
            entrypoint = _get_entrypoint(node)
            environment = _process_environment(
                    node.get('environment'),
                    '{}.{}'.format(name, 'environment'))
            shell = node.get('shell')
            as_root = node.get('root', as_root)

        return cls(name, script, image, entrypoint, environment, shell, as_root)


class ScubaConfig:
    def __init__(self, **data):
        optional_nodes = ('image','aliases','hooks','entrypoint','environment','shell')

        # Check for unrecognized nodes
        extra = [n for n in data if not n in optional_nodes]
        if extra:
            raise ConfigError('{}: Unrecognized node{}: {}'.format(SCUBA_YML,
                    's' if len(extra) > 1 else '', ', '.join(extra)))

        self._image = data.get('image')
        self._shell = data.get('shell', DEFAULT_SHELL)
        self._entrypoint = _get_entrypoint(data)
        self._load_aliases(data)
        self._load_hooks(data)
        self._environment = self._load_environment(data)




    def _load_aliases(self, data):
        self._aliases = {}

        for name, node in data.get('aliases', {}).items():
            if ' ' in name:
                raise ConfigError('Alias names cannot contain spaces')
            self._aliases[name] = ScubaAlias.from_dict(name, node)


    def _load_hooks(self, data):
        self._hooks = {}

        for name in ('user', 'root',):
            node = data.get('hooks', {}).get(name)
            if node:
                hook = _process_script_node(node, name)
                self._hooks[name] = hook

    def _load_environment(self, data):
         return _process_environment(data.get('environment'), 'environment')


    @property
    def image(self):
        if not self._image:
            raise ConfigError("Top-level 'image' not set")
        return self._image

    @property
    def entrypoint(self):
        return self._entrypoint

    @property
    def aliases(self):
        return self._aliases

    @property
    def hooks(self):
        return self._hooks

    @property
    def environment(self):
        return self._environment

    @property
    def shell(self):
        return self._shell




def load_config(path):
    try:
        with open(path, 'r') as f:
            data = yaml.load(f, Loader)
    except IOError as e:
        raise ConfigError('Error opening {}: {}'.format(SCUBA_YML, e))
    except yaml.YAMLError as e:
        raise ConfigError('Error loading {}: {}'.format(SCUBA_YML, e))

    return ScubaConfig(**(data or {}))
