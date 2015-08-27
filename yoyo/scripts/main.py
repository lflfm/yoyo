# Copyright 2015 Oliver Cope
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
from copy import copy
import logging
import argparse
import os
import sys

from yoyo.compat import NoOptionError
from yoyo.config import (CONFIG_FILENAME,
                         find_config,
                         read_config,
                         save_config,
                         update_argparser_defaults)
from yoyo import utils
from yoyo import default_migration_table
from yoyo import logger

verbosity_levels = {
    0: logging.ERROR,
    1: logging.WARN,
    2: logging.INFO,
    3: logging.DEBUG
}

min_verbosity = min(verbosity_levels)
max_verbosity = max(verbosity_levels)

LEGACY_CONFIG_FILENAME = '.yoyo-migrate'



class InvalidArgument(Exception):
    pass


def parse_args(argv=None):
    """
    Parse the config file and command line args.

    :return: tuple of (argparser, parsed_args)
    """
    #: List of arguments whose defaults should be read from the config file
    config_args = {'batch_mode': 'getboolean',
                   'sources': 'get',
                   'database': 'get',
                   'verbosity': 'getint',
                   }

    globalparser, argparser, subparsers = make_argparser()

    # Initial parse to extract --config and any global arguments
    global_args, _ = globalparser.parse_known_args(argv)

    # Read the config file and create a dictionary of defaults for argparser
    config = read_config(global_args.config or
                         find_config()
                         if global_args.use_config_file
                         else None)

    defaults = {}
    for argname, getter in config_args.items():
        try:
            defaults[argname] = getattr(config, getter)('DEFAULT', argname)
        except NoOptionError:
            pass

    # Set the argparser defaults to values read from the config file
    update_argparser_defaults(globalparser, defaults)
    update_argparser_defaults(argparser, defaults)
    for subp in subparsers.choices.values():
        update_argparser_defaults(subp, defaults)

    # Now parse for real, starting from the top
    args = argparser.parse_args(argv)

    # Update the args namespace with the global args.
    # This ensures that global args (eg '-v) are recognized regardless
    # of whether they were placed before or after the subparser command.
    # If we do not do this then the sub_parser copy of the argument takes
    # precedence, and overwrites any global args set before the command name.
    args.__dict__.update(globalparser.parse_known_args(argv)[0].__dict__)

    return config, argparser, args


def make_argparser():
    """
    Return a top-level ArgumentParser parser object,
    plus a list of sub_parsers
    """
    global_parser = argparse.ArgumentParser(add_help=False)
    global_parser.add_argument("--config", "-c", default=None,
                               help="Path to config file")
    global_parser.add_argument("-v",
                               dest="verbosity",
                               action="count",
                               default=min_verbosity,
                               help="Verbose output. Use multiple times "
                               "to increase level of verbosity")
    global_parser.add_argument("-b",
                               "--batch",
                               dest="batch_mode",
                               action="store_true",
                               help="Run in batch mode"
                               ". Turns off all user prompts")

    global_parser.add_argument("--no-config-file", "--no-cache",
                               dest="use_config_file",
                               action="store_false",
                               default=True,
                               help="Don't look for a .yoyorc config file")
    argparser = argparse.ArgumentParser(prog='yoyo-migrate',
                                        parents=[global_parser])

    subparsers = argparser.add_subparsers(help='Commands help')

    from . import migrate
    migrate.install_argparsers(global_parser, subparsers)

    return global_parser, argparser, subparsers


def configure_logging(level):
    """
    Configure the python logging module with the requested loglevel
    """
    logging.basicConfig(level=verbosity_levels[level])


def prompt_save_config(config, path):
    # Offer to save the current configuration for future runs
    # Don't cache anything in batch mode (because we can't prompt to find the
    # user's preference).

    if utils.confirm("Save migration configuration to {}?\n"
                     "This is saved in plain text and "
                     "contains your database password.\n\n"
                     "Answering 'y' means you do not have to specify "
                     "the migration source or database connection "
                     "for future runs".format(path)):
        save_config(config, path)


def upgrade_legacy_config(args, config, sources):

    for dir in reversed(sources):
        path = os.path.join(dir, LEGACY_CONFIG_FILENAME)
        if not os.path.isfile(path):
            continue

        legacy_config = read_config(path)

        def transfer_setting(oldname, newname,
                             transform=None, section='DEFAULT'):
            try:
                config.get(section, newname)
            except NoOptionError:
                try:
                    value = legacy_config.get(section, oldname)
                except NoOptionError:
                    pass
                else:
                    if transform:
                        value = transform(value)
                    config.set(section, newname, value)

        transfer_setting('dburi', 'database')
        transfer_setting('migration_table', 'migration_table',
                         lambda v: (default_migration_table
                                    if v == 'None'
                                    else v))

        config_path = args.config or CONFIG_FILENAME
        if not args.batch_mode:
            if utils.confirm("Move legacy configuration in {!r} to {!r}?"
                             .format(path, config_path)):
                save_config(config, config_path)
            try:
                if utils.confirm("Delete legacy configuration file {!r}"
                                 .format(path)):
                    os.unlink(path)
            except OSError:
                logger.warn("Could not remove %r. Manually remove this file "
                            "to avoid future warnings", path)
        else:
            logger.warn("Found legacy configuration in %r. Run "
                        "yoyo-migrate in interactive mode to update your "
                        "configuration files", path)

        try:
            args.database = (
                    args.database or legacy_config.get('DEFAULT', 'dburi'))
        except NoOptionError:
            pass
        try:
            args.migration_table = (
                args.migration_table or
                legacy_config.get('DEFAULT', 'migration_table'))
        except NoOptionError:
            pass


def main(argv=None):
    config, argparser, args = parse_args(argv)
    config_is_empty = (config.sections() == [] and
                       config.items('DEFAULT') == [])

    sources = getattr(args, 'sources', None)
    if sources:
        if upgrade_legacy_config(args, config, sources.split()):
            return main(argv)

    verbosity = args.verbosity
    verbosity = min(max_verbosity, max(min_verbosity, verbosity))
    configure_logging(verbosity)

    command_args = (args,)
    for f in args.funcs:
        print(f)
        try:
            result = f(*command_args)
        except InvalidArgument as e:
            argparser.error(e.args[0])

        if result is not None:
            command_args += result

    if config_is_empty and args.use_config_file and not args.batch_mode:
        config.set('DEFAULT', 'sources', args.sources)
        config.set('DEFAULT', 'database', args.database)
        config.set('DEFAULT', 'migration_table', args.migration_table)
        config.set('DEFAULT', 'batch_mode', 'off' if args.batch_mode else 'on')

        prompt_save_config(config, args.config or CONFIG_FILENAME)


if __name__ == "__main__":
    main(sys.argv[1:])