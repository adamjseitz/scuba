SCUBA  [![Build Status](https://travis-ci.org/JonathonReinhart/scuba.svg?branch=master)](https://travis-ci.org/JonathonReinhart/scuba) [![codecov.io](https://codecov.io/github/JonathonReinhart/scuba/coverage.svg?branch=master)](https://codecov.io/github/JonathonReinhart/scuba?branch=master) [![PyPI](https://img.shields.io/pypi/v/scuba.svg)](https://pypi.python.org/pypi/scuba)
-----

Simple Container-Utilizing Build Apparatus

SCUBA is a simple tool that makes it easier to use Docker containers in everyday development.
It is intended to be used by developers in 'make' or 'scons' based build environments, where
the entire build environment is encapsulated in a Docker container.

Its purpose is to lower the barrier to using Docker for everyday builds. SCUBA keeps you from
having to remember a complex `docker run` command line, and turns this

    $ docker run -it --rm -v $(pwd):/build:z -w /build -u $(id -u):$(id -g) gcc:5.1 make myprogram

into this:

    $ scuba make myprogram

## Installation

### Install via pip
Scuba is [hosted at PyPI](https://pypi.python.org/pypi/scuba).

To install:

    $ sudo pip install scuba

To uninstall:

    $ sudo pip uninstall scuba

### Install from source
Scuba can be built from source on Linux only (due to the fact that `scubainit`
must be compiled):

1. Run `make` to build `scubainit`
2. Run `./run_nosetests.sh` to run the unit tests
3. Run `sudo python setup.py install` to install scuba
4. Run `./run_full_tests.py` to test the installed version of scuba

If [musl-libc] is installed, it can be used to reduce the size of `scubainit`,
by overriding the `CC` environment variable in step 1:

`CC=/usr/local/musl/bin/musl-gcc make`


## Configuration

Configuration is done using a [YAML](http://yaml.org/) file named `.scuba.yml` in the root
directory of your project. It is expected that `.scuba.yml` be checked in to version control.
Full documentation on [`.scuba.yml`] can be found in [`doc/yaml-reference.md`](doc/yaml-reference.md),
and specific examples can be found in the [`example`](example/) directory.

An example `.scuba.yml` file might look like this:

```yaml
image: gcc:5.1

aliases:
  build: make -j4
```

In this example, `scuba build foo` would execute `make -j4 foo` in a `gcc:5.1` container.

## License

This software is released under the [MIT License](https://opensource.org/licenses/MIT).




[musl-libc]: https://www.musl-libc.org/
