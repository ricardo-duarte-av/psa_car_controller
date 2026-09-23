import locale
import logging
import sys

import dash_bootstrap_components as dbc
from flask import Flask
from werkzeug import run_simple
from werkzeug.middleware.proxy_fix import ProxyFix

from dash import Dash
try:
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
except ImportError:
    from werkzeug import DispatcherMiddleware

from psa_car_controller.common.mylogger import file_handler
if sys.version_info >= (3, 8):
    import importlib
else:
    import importlib_metadata as importlib

# pylint: disable=invalid-name
app = None
dash_app = None

FONTS_URL = "https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700" \
            "&family=Barlow+Semi+Condensed:wght@500;600&display=swap"
# sets the theme (the one chosen, else the browser's) and the dashboard's section before the page
# draws, so it doesn't flash; assets/theme.js keeps them up to date afterwards
INDEX_STRING = """<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        <script>
            (function () {
                var choice = "auto";
                try { choice = localStorage.getItem("psacc-theme") || "auto"; } catch (e) {}
                if (["light", "dark", "auto"].indexOf(choice) < 0) choice = "auto";
                var dark = choice === "dark" ||
                    (choice === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
                var root = document.documentElement;
                root.setAttribute("data-theme-choice", choice);
                root.setAttribute("data-bs-theme", dark ? "dark" : "light");
                var tab = window.location.hash.replace("#", "");
                root.setAttribute("data-tab", tab || "summary");
            })();
        </script>
        {%css%}
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>"""

logger = logging.getLogger(__name__)


class MyProxyFix(ProxyFix):
    def __init__(self, dashapp: Dash, static_prefix):
        self.flask_app = dashapp.server
        self.dash_app = dashapp
        self.static_prefix = static_prefix
        super().__init__(self.flask_app.wsgi_app, x_host=1, x_port=1, x_prefix=1)

    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_INGRESS_PATH") or environ.get("HTTP_X_FORWARDED_PREFIX") or self.static_prefix
        self.dash_app.config.__dict__["_read_only"] = []

        if prefix == "/":
            self.dash_app.config.requests_pathname_prefix = ""
            self.dash_app.config.url_base_pathname = None
        else:
            environ["HTTP_X_FORWARDED_PREFIX"] = prefix
            if not prefix.endswith("/"):
                prefix += "/"
            self.dash_app.config.requests_pathname_prefix = prefix
            self.dash_app.config.url_base_pathname = prefix
        self.dash_app.config.assets_external_path = prefix

        return super().__call__(environ, start_response)


def start_app(*args, **kwargs):
    run(config_flask(*args, **kwargs))


def config_flask(title, base_path, debug: bool, host, port, reloader=False,
                 # pylint: disable=too-many-arguments,too-many-positional-arguments
                 unminified=False, view="psa_car_controller.web.view.views"):
    global app, dash_app
    reload_view = app is not None
    app = Flask(__name__)
    app.logger.addHandler(file_handler)
    try:
        lang = locale.getlocale()[0].split("_")[0]
        locale.setlocale(locale.LC_TIME, ".".join(locale.getlocale()))  # make sure LC_TIME is set
        if lang != "en":
            locale_url = [f"https://cdn.plot.ly/plotly-locale-{lang}-latest.js"]
        else:
            locale_url = None
    except (IndexError, locale.Error):
        locale_url = None
        logger.warning("Can't get language")
    if unminified:
        locale_url = ["assets/plotly-with-meta.js"]
    app.config["DEBUG"] = debug
    if base_path == "/":
        application = DispatcherMiddleware(app)
        requests_pathname_prefix = "/"
    else:
        application = DispatcherMiddleware(Flask('dummy_app'), {base_path: app})
        requests_pathname_prefix = base_path + "/"
    dash_app = Dash(external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP, FONTS_URL],
                    external_scripts=locale_url, title=title,
                    server=app, requests_pathname_prefix=requests_pathname_prefix,
                    suppress_callback_exceptions=True)
    dash_app.index_string = INDEX_STRING
    dash_app.enable_dev_tools(debug)
    app.wsgi_app = MyProxyFix(dash_app, base_path)
    # keep this line
    importlib.import_module(view)
    if reload_view:
        importlib.reload(view)
    # threaded is needed to keep serving requests while /events streams are open
    return {"hostname": host, "port": port, "application": application, "use_reloader": reloader,
            "use_debugger": debug, "threaded": True}


def run(config):
    return run_simple(**config)
