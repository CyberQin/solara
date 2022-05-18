import os
import site
import sys
import threading
import time
import typing
import webbrowser
from enum import Enum
from pathlib import Path

import react_ipywidgets
import rich
import rich_click as click
import uvicorn
from uvicorn.main import LEVEL_CHOICES

import solara.server

from .server import settings

HOST_DEFAULT = os.environ.get("HOST", "localhost")
if "arm64-apple-darwin" in HOST_DEFAULT:  # conda activate script
    HOST_DEFAULT = "localhost"

LOGGING_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": None,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',  # noqa: E501
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "rich": {
            "class": "rich.logging.RichHandler",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "solara": {"handlers": ["rich"], "level": "INFO"},
        # "react": {"handlers": ["rich"], "level": "DEBUG"},
        "uvicorn": {"handlers": ["default"], "level": "ERROR"},
        "uvicorn.error": {"level": "ERROR"},
        "uvicorn.access": {"handlers": ["access"], "level": "ERROR", "propagate": False},
    },
}


def find_all_packages_paths():
    paths = []
    # sitepackages = set([os.path.dirname(k) for k in site.getsitepackages()])
    sitepackages = set([k for k in site.getsitepackages()])
    paths.extend(list(sitepackages))
    print(sitepackages)
    for name, module in sys.modules.items():
        if hasattr(module, "__path__"):
            try:
                path = module.__path__[0]
            except:  # noqa: E722
                pass  # happens for namespace packages it seems
                # print(f"Error for {name}")
                # if path:
                #     skip = False
                #     for sitepackage in sitepackages:
                #         if path.startswith(sitepackage):
                #             skip = True
                # if not skip:
                # print(name, path, skip)
                paths.append(str(path))
    # print("PATHS", paths)
    return paths


class EnumType(click.Choice):
    def __init__(self, enum: typing.Type[Enum], case_sensitive=False):
        self._enum = enum
        super().__init__(choices=[item.name for item in enum], case_sensitive=case_sensitive)


@click.group()
def cli():
    pass


@cli.command()
@click.option("--port", default=int(os.environ.get("PORT", 8765)))
@click.option("--host", default=HOST_DEFAULT)
@click.option("--dev/--no-devn", default=False)
@click.option("--open/--no-open", default=False)
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload.")
@click.option(
    "--reload-dir",
    "reload_dirs",
    multiple=True,
    help="Set reload directories explicitly, instead of using the current working" " directory.",
    type=click.Path(exists=True),
)
@click.option(
    "--reload-exclude",
    "reload_excludes",
    multiple=True,
    help="Set glob patterns to exclude while watching for files. Includes "
    "'.*, .py[cod], .sw.*, ~*' by default; these defaults can be overridden "
    "with `--reload-include`. This option has no effect unless watchgod is "
    "installed.",
)
@click.option(
    "--workers",
    default=None,
    type=int,
    help="Number of worker processes. Defaults to the $WEB_CONCURRENCY environment" " variable if available, or 1. Not valid with --reload.",
)
@click.option(
    "--env-file",
    type=click.Path(exists=True),
    default=None,
    help="Environment configuration file.",
    show_default=True,
)
@click.option(
    "--log-config",
    type=click.Path(exists=True),
    default=None,
    help="Logging configuration file. Supported formats: .ini, .json, .yaml.",
    show_default=True,
)
@click.option(
    "--log-level",
    type=LEVEL_CHOICES,
    default=None,
    help="Log level. [default: info]",
    show_default=True,
)
@click.option(
    "--log-level-uvicorn",
    type=LEVEL_CHOICES,
    default="error",
    help="Log level. [default: error]",
    show_default=True,
)
@click.option(
    "--access-log/--no-access-log",
    is_flag=True,
    default=True,
    help="Enable/Disable access log.",
)
@click.option(
    "--root-path",
    type=str,
    default="",
    help="Set the ASGI 'root_path' for applications submounted below a given URL path.",
)
@click.option(
    "--theme-loader",
    type=str,
    default=settings.theme.loader,
    help=f"Loader to use when the app is not yet shown to the user. [default: {settings.theme.loader!r}]",
)
@click.option(
    "--theme-variant",
    type=settings.ThemeVariant,
    default=settings.ThemeVariant.light.name,
    help=f"Use light or dark variant, or auto detect (auto). [default: {settings.theme.variant.name}",
)
@click.option(
    "--theme-variant-user-selectable/--no-theme-variant-user-selectable",
    type=bool,
    default=settings.theme.variant_user_selectable,
    help=f"Can the user select the theme variant from the UI. [default: {settings.theme.variant_user_selectable}",
)
@click.option("--pdb/--no-pdb", "use_pdb", default=False, help="Enter debugger on error")
@click.argument("app")
def run(
    app,
    host,
    port,
    open,
    reload: bool,
    reload_dirs: typing.Optional[typing.List[str]],
    dev: bool,
    reload_excludes: typing.List[str],
    workers: int,
    env_file: str,
    root_path: str,
    log_config: str,
    log_level: str,
    log_level_uvicorn: str,
    access_log: bool,
    use_pdb: bool,
    theme_loader: str,
    theme_variant: settings.ThemeVariant,
    theme_variant_user_selectable: bool,
):
    reload_dirs = reload_dirs if reload_dirs else None
    url = f"http://{host}:{port}"

    failed = False
    if dev:
        solara_root = Path(solara.__file__).parent

        reload_dirs = list(reload_dirs if reload_dirs else [])

        # we restart the server when solara or react changes, in priciple we should do
        # that for all dependencies of the server, but these are changing most often
        # during development
        # We exclude exampes, that will be handled by solara/server/reload.py
        reload_dirs = [str(solara_root), str(Path(react_ipywidgets.__file__).parent)]
        reload_excludes = reload_excludes if reload_excludes else []
        reload_excludes = [str(solara_root / "examples")]
        del solara_root
        reload = True

    server = None

    # TODO: we might want to support this, but it needs to be started from the main thread
    # and then uvicorn needs to be started from a thread
    # def open_webview():
    #     import webview

    #     while not failed and (server is None or not server.started):
    #         time.sleep(0.1)
    #     if not failed:
    #         window = webview.create_window("Hello world", url, resizable=True)
    #         window.on_top = True
    #         # window.show()
    #         webview.start(debug=True)

    def open_browser():
        while not failed and (server is None or not server.started):
            time.sleep(0.1)
        if not failed:
            webbrowser.open(url)

    if open:
        threading.Thread(target=open_browser, daemon=True).start()
    rich.print(f"Solara server is starting at {url}")

    if log_level is not None:
        LOGGING_CONFIG["loggers"]["solara"]["level"] = log_level.upper()

    log_level = log_level_uvicorn
    del log_level_uvicorn

    kwargs = locals().copy()
    os.environ["SOLARA_APP"] = app
    kwargs["app"] = "solara.server.starlette:app"
    kwargs["log_config"] = LOGGING_CONFIG if log_config is None else log_config
    settings.main.use_pdb = use_pdb
    settings.theme.loader = theme_loader
    settings.theme.variant = theme_variant
    settings.theme.variant_user_selectable = theme_variant_user_selectable
    for item in "theme_variant_user_selectable theme_variant theme_loader use_pdb server open_browser open url failed dev".split():
        del kwargs[item]

    def start_server():
        nonlocal server
        nonlocal failed
        try:
            # we manually create the server instead of calling uvicorn.run
            # because we can then access the server variable and check if it is
            # running.
            config = uvicorn.Config(**kwargs)
            server = uvicorn.Server(config=config)
            if reload:
                sock = config.bind_socket()
                from uvicorn.supervisors import ChangeReload

                ChangeReload(config, target=server.run, sockets=[sock]).run()
            else:
                server.run()
        except:  # noqa
            failed = True
            raise

    start_server()

    # TODO: if we want to use webview, it should be sth like this
    # server_thread = threading.Thread(target=start_server)
    # server_thread.start()
    # if open:
    #     # open_webview()
    #     open_browser()
    # server_thread.join()


cli.add_command(run)


def main():
    cli()


if __name__ == "__main__":
    main()
