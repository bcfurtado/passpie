# -*- coding: utf-8 -*-
from copy import deepcopy
from tempfile import mkdtemp, NamedTemporaryFile
import functools
import json
import logging
import os
import shutil


from tabulate import tabulate
from tinydb.storages import touch
from tinydb import Query
import click

from . import importers
from .config import Config
from .database import (
    Database,
    CredentialFactory,
    split_fullname,
    make_fullname
)
from .gpg import generate_keys, export_keys, GPG
from .proc import run
from .utils import (
    auto_archive,
    copy_to_clipboard,
    genpass,
    logger,
    make_archive,
    safe_join,
    which,
    yaml_dump,
    yaml_to_python,
)
from .git import Repo


__version__ = "2.0"


#############################
# table
#############################

class Table(object):

    def __init__(self, config):
        self.table_format = config["TABLE_FORMAT"]
        self.show_password = config["TABLE_SHOW_PASSWORD"]
        self.hidden_string = config["TABLE_HIDDEN_STRING"]
        self.style = config["TABLE_STYLE"]
        self.fields = config["TABLE_FIELDS"]

    def colorize(self, data):
        for field, style in self.style.items():
            for elem in data:
                elem[field] = click.style(elem[field], **style)
        return data

    def hide_password(self, data):
        for elem in data:
            elem["password"] = self.hidden_string
        return data

    def render(self, data):
        data = deepcopy(data)
        data = self.colorize(data)
        if not self.show_password:
            data = self.hide_password(data)
        ordered_data = []
        for elem in data:
            ordered_data.append(tuple(elem[f] for f in self.fields))
        headers = tuple(click.style(f.title(), bold=True) for f in self.fields)
        return tabulate(
            ordered_data,
            headers=headers,
            tablefmt=self.table_format,
            numalign="left",
        )


#############################
# cli
#############################


def pass_database(ensure_passphrase=False, confirm_passphrase=False, ensure_exists=True, sync=True):
    def decorator(command):
        @functools.wraps(command)
        def wrapper(*args, **kwargs):
            context = click.get_current_context()
            config_overrides = context.meta["config_overrides"]
            passphrase = context.meta["passphrase"]
            if ensure_passphrase and not passphrase:
                passphrase = click.prompt(
                    "Passphrase",
                    hide_input=True,
                    confirmation_prompt=confirm_passphrase)
            database_path = config_overrides.get(
                "DATABASE", Config.DEFAULT["DATABASE"])
            try:
                with auto_archive(database_path) as archive:
                    config_path = safe_join(archive.path, "config.yml")
                    keys_path = safe_join(archive.path, "keys.yml")
                    cfg = Config(config_path, config_overrides)
                    gpg = GPG(keys_path,
                              passphrase,
                              cfg["GPG_HOMEDIR"],
                              cfg["GPG_RECIPIENT"])
                    with Database(archive, cfg, gpg) as db:
                        return command(db, *args, **kwargs)
            except (IOError, ValueError) as exception:
                raise
                raise click.ClickException("{}".format(exception))
        return wrapper
    return decorator


def validate_cols(ctx, param, value):
    if value:
        try:
            validated = {c: index for index, c in enumerate(value.split(',')) if c}
            for col in ('name', 'login', 'password'):
                assert col in validated
            return validated
        except (AttributeError, ValueError):
            raise click.BadParameter('cols need to be in format col1,col2,col3')
        except AssertionError as e:
            raise click.BadParameter('missing mandatory column: {}'.format(e))


def validate_yaml_str(ctx, param, value):
    if value:
        try:
            yaml_to_python(
                value)
            return value
        except ValueError:
            raise click.BadParameter('not a valid yaml string: {}'.format(value))


def prompt_update(credential, field, hidden=False):
    value = credential[field]
    prompt = "{} [{}]".format(field.title(), "*****" if hidden else value)
    return click.prompt(prompt,
                        hide_input=hidden,
                        confirmation_prompt=hidden,
                        default=value,
                        show_default=False)


@click.group()
@click.option("-P", "--passphrase", help="Database passphrase")
@click.option("-R", "--recipient", help="Database recipient")
@click.option("-D", "--database", help="Database path")
@click.option("-g", "--git-push", help="Autopush git [origin/master]")
@click.option('-v', '--verbose', count=True, help='Activate verbose output', envvar="PASSPIE_VERBOSE")
@click.option('--debug', is_flag=True, help='Activate debug output', envvar="PASSPIE_DEBUG")
@click.version_option(__version__)
@click.pass_context
def cli(ctx, database, passphrase, recipient, git_push, verbose, debug):
    config_overrides = {}
    if database:
        config_overrides["DATABASE"] = database
    if git_push:
        config_overrides["GIT_PUSH"] = git_push
    if recipient:
        config_overrides["GPG_RECIPIENT"] = recipient
    ctx.meta["config_overrides"] = config_overrides
    ctx.meta["passphrase"] = passphrase

    if verbose is 1:
        logger.setLevel(logging.INFO)
    if debug is True or verbose > 1:
        logger.setLevel(logging.DEBUG)


@cli.command()
@click.argument("path", default="passpie.db")
@click.option("-f", "--force", is_flag=True, help="Force initialization")
@click.option("-r", "--recipient", help="Keyring recipient")
@click.option("--key-length", type=click.Choice(["1024", "2048", "4096"]), help="Key length")
@click.option("--expire-date", help="Key expiration date")
@click.option("-ng", "--no-git", is_flag=True, help="Don't initialize a git repo")
@click.option("-F", "--format", default="gztar", type=click.Choice(["dir", "tar", "zip", "gztar", "bztar"]))
@click.pass_context
def init(ctx, path, force, recipient, no_git, key_length, expire_date, format):
    """Initialize database"""
    config = Config(Config.GLOBAL_PATH, ctx.meta["config_overrides"])
    passphrase = ctx.meta["passphrase"]
    if not passphrase and not recipient:
        passphrase = click.prompt(
            "Passphrase",
            hide_input=True,
            confirmation_prompt=True,
        )

    if os.path.exists(path):
        if force and os.path.isdir(path):
            shutil.rmtree(path)
        elif force and os.path.isfile(path):
            os.remove(path)
        else:
            msg = "Path '{}' exists [--force] to overwrite".format(path)
            raise click.ClickException(msg)

    tempdir = mkdtemp()
    config_values = {}
    if recipient:
        config_values["RECIPIENT"] = recipient
    else:
        key_values = {
            "key_length": key_length or 4096,
            "passphrase": passphrase,
            "expire_date": expire_date or 0,
        }
        homedir = mkdtemp()
        keys_data = generate_keys(homedir, key_values)
        keys_content = export_keys(homedir, keys_data["email"])
        keys_file_path = safe_join(tempdir, "keys.yml")
        yaml_dump([keys_content], keys_file_path)

    dot_passpie_file_path = safe_join(tempdir, ".passpie")
    touch(dot_passpie_file_path)

    config_file_path = safe_join(tempdir, "config.yml")
    yaml_dump(config_values, config_file_path)

    if no_git or config["GIT"] is False:
        pass  # Don't create a git repo
    else:
        repo = Repo(tempdir)
        repo.init().commit("Initialize database")
    make_archive(src=tempdir, dest=path, dest_format=format)


@cli.command(name="list")
@pass_database(sync=False)
@click.argument("grep", required=False)
def listdb(db, grep):
    """List credentials as table"""
    if grep:
        query = (Query().login.search(".*{}.*".format(grep)) |
                 Query().name.search(".*{}.*".format(grep)) |
                 Query().comment.search(".*{}.*".format(grep)))
        credentials = db.search(query)
    else:
        credentials = db.all()

    if credentials:
        table = Table(db.config)
        click.echo(table.render(credentials))


@cli.command(name="config")
@click.argument("name", required=False, type=str)
@click.argument("value", required=False, type=str, callback=validate_yaml_str)
@pass_database(sync=False)
def config_database(db, name, value):
    """Configuration settings"""
    name = name.upper() if name else ""
    if name and name in db.config.keys():
        if value:
            db.config[name] = yaml_to_python(value)
            db.repo.commit("Set config: {} = {}".format(name, value))
        else:
            click.echo("{}".format(db.config[name]))
    else:
        config_content = yaml_dump(dict(db.config.data)).strip()
        click.echo(config_content)


@cli.command()
@click.argument("fullnames", nargs=-1, callback=lambda ctx, param, val: list(val))
@click.option("-r", "--random", is_flag=True, help="Random password generation")
@click.option("-c", "--comment", default="", help="Credentials comment")
@click.option("-p", "--password", help="Credentials password")
@click.option("-f", "--force", is_flag=True, help="Force overwrite existing credential")
@click.option("--fake", is_flag=True, help="Add fake credential")
@pass_database()
def add(db, fullnames, random, comment, password, force, fake):
    """Insert credential"""
    if fake:
        fake_cred = CredentialFactory()
        db.insert(db.encrypt(fake_cred))
        fullname = make_fullname(fake_cred["login"], fake_cred["name"])
        db.repo.commit("Add fake credential '{}'".format(fullname))
    elif random or db.config["PASSWORD_RANDOM"]:
        password = genpass(db.config["PASSWORD_PATTERN"],
                           db.config["PASSWORD_RANDOM_LENGTH"])
    elif password is None:
        password = click.prompt(
            "Password", hide_input=True, confirmation_prompt=True)
    for fullname in fullnames:
        login, name = split_fullname(fullname)
        credential = {
            "name": name,
            "login": login,
            "password": password,
            "comment": comment,
        }
        if db.contains(db.query(fullname)):
            if force:
                db.update(db.encrypt(credential), db.query(fullname))
            else:
                msg = "Credential {} exists. `--force` to overwrite".format(fullname)
                raise click.ClickException(msg)
        else:
            db.insert(db.encrypt(credential))

    db.repo.commit("Add credentials '{}'".format((", ").join(fullnames)))


@cli.command()
@click.argument("fullnames", nargs=-1, callback=lambda ctx, param, val: list(val))
@click.option("-f", "--force", is_flag=True, help="Skip confirmation prompt")
@click.option("-A", "--all", "purge", is_flag=True, help="Purge all credentials")
@pass_database()
def remove(db, fullnames, force, purge):
    """Remove credential"""
    if purge:
        db.purge()
        db.repo.commit("Purge credentials")
    else:
        removed = False
        fulnames = [f for f in fullnames if db.contains(db.query(f))]
        for fullname in fulnames:
            if force or click.confirm("Remove {}".format(fullname)):
                db.remove(db.query(fullname))
                removed = True
        if removed is True:
            msg = "Remove credentials '{}'".format((", ").join(fullnames))
            db.repo.commit(msg)


@cli.command()
@click.argument("fullnames", nargs=-1, callback=lambda ctx, param, val: list(val))
@click.option("-r", "--random", is_flag=True, help="Random password generation")
@click.option("-P", "--pattern", help="Random password pattern")
@click.option("-C", "--copy", is_flag=True, help="Copy passwor to clipboard")
@click.option("-i", "--interactive", is_flag=True, help="Interactive confirm updates")
@click.option("-c", "--comment", help="Credentials comment")
@click.option("-p", "--password", help="Credentials password")
@click.option("-l", "--login", help="Credentials login")
@click.option("-n", "--name", help="Credentials name")
@pass_database(ensure_passphrase=True)
def update(db, fullnames, random, pattern, copy, comment, password, name, login, interactive):
    """Update credential"""
    updated = []
    for fullname in [f for f in fullnames if db.contains(db.query(f))]:
        if interactive is True and not click.confirm("Update {}".format(fullname)):
            # Don't update
            continue
        cred = db.get(db.query(fullname))
        values = {}
        if login:
            values["login"] = login
        if name:
            values["name"] = name
        if password:
            values["password"] = db.gpg.encrypt(password)
        if comment:
            values["comment"] = comment
        if random:
            values["password"] = genpass(pattern or db.config["PASSWORD_PATTERN"])

        if not values:
            values["login"] = prompt_update(cred, "login")
            values["name"] = prompt_update(cred, "name")
            values["password"] = prompt_update(cred, "password", hidden=True)
            values["comment"] = prompt_update(cred, "comment")

        db.update(values, db.query(fullname))
        updated.append(fullname)

    if updated:
        db.repo.commit("Update credentials '{}'".format((", ").join(fullnames)))

        if copy:
            password = db.gpg.decrypt(cred["password"])
            copy_to_clipboard(password, db.config["COPY_TIMEOUT"])


@cli.command()
@click.argument("fullname")
@click.argument("dest", type=click.File("w"), required=False)
@click.option("-t", "--timeout", type=int, default=0, help="Timeout to clear clipboard")
@pass_database(ensure_passphrase=True)
def copy(db, fullname, dest, timeout):
    """Copy credential password"""
    credential = db.get(db.query(fullname))
    timeout = timeout or db.config["COPY_TIMEOUT"]
    if credential:
        credential = db.decrypt(credential)
        password = credential["password"]
        if dest:
            dest.write(password)
        else:
            copy_to_clipboard(password, timeout)
    else:
        raise click.ClickException("{} not found".format(fullname))


@cli.command(name="import")
@click.argument("filepath", required=False, type=click.Path(readable=True, exists=True, allow_dash=True))
@click.option("-I", "--importer", type=click.Choice(importers.get_names()),
              help="Specify an importer")
@click.option("--csv", help="CSV expected columns", callback=validate_cols)
@click.option("--skip-lines", type=int, help="Number of lines to skip")
@click.option("-f", "--force", is_flag=True, help="Force importing credentials")
@pass_database()
def import_database(db, filepath, importer, csv, force, skip_lines):
    """Import credentials in plain text"""
    tempfile = None
    if filepath is None or filepath == "-":
        tempfile = NamedTemporaryFile()
        with open(tempfile.name, "w") as stdinfile:
            stdinfile.write(click.get_text_stream('stdin').read())
        filepath = tempfile.name

    if csv:
        importer = importers.get(name='csv')
        params = {'cols': csv, 'skip_lines': skip_lines}
    else:
        importer = importers.find_importer(filepath)
        params = {}

    if importer:
        imported = False
        credentials = importer.handle(filepath, params=params)
        for credential in [db.encrypt(c) for c in credentials]:
            fullname = make_fullname(credential["login"], credential["name"])
            if db.contains(db.query(fullname)):
                if force is False and not click.confirm("Update {}".format(fullname)):
                    continue
                else:
                    db.update(credential, db.query(fullname))
                    imported = True
            else:
                db.insert(credential)
                imported = True

        if imported is True:
            if tempfile:
                message = 'Imported credentials from stdin'
            else:
                message = u'Imported credentials from {}'.format(filepath)
            db.repo.commit(message=message)


@cli.command(name="export")
@click.argument("filepath", type=click.File("w"))
@click.option("--json", "as_json", is_flag=True, help="Export as JSON")
@pass_database(ensure_passphrase=True, sync=False)
def export_database(db, filepath, as_json):
    """Export credentials in plain text"""
    credentials = [dict(db.decrypt(c)) for c in db.all()]
    if as_json:
        content = json.dumps(credentials, indent=2)
    else:
        content = yaml_dump(credentials)
    filepath.write(content)


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1)
@pass_database()
def git(db, command):
    """Git commands"""
    cmd = ["git"] + list(command)
    run(cmd, cwd=db.path, pipe=False)


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1)
@click.option("--gen-key", is_flag=True, help="Generate keys")
@pass_database()
def gpg(db, command, gen_key):
    """GPG commands"""
    if gen_key:
        # values = {
        #     "key_length": click.prompt("Key length", default=4096),
        #     "name": click.prompt("Name"),
        #     "email": click.prompt("Email"),
        #     "comment": click.prompt("Comment", default=""),
        #     "passphrase": click.prompt("Passphrase", hide_input=True, confirmation_prompt=True),
        #     "expire_date": click.prompt("Expire date", default=0),
        # }
        values = {
            "key_length": 4096,
            "name": "name",
            "email": "email@example",
            "comment": "comment",
            "passphrase": "p",
            "expire_date": 0,
        }
        keys_data = generate_keys(db.gpg.homedir, values)
        keys_content = export_keys(db.gpg.homedir, keys_data["email"])
        yaml_dump(db.gpg.keys + [keys_content], db.gpg.path)
    else:
        cmd = [which("gpg2", "gpg"), "--homedir", db.gpg.homedir] + list(command)
        run(cmd, cwd=db.path, pipe=False)
