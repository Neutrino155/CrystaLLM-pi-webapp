import crystal_toolkit.components as ctc
import dash
from dash import dcc
from dash import html
from dash.dependencies import Input, Output, State
import dash_bootstrap_components as dbc
from flask import request, send_from_directory
from pymatgen.core import Composition
from pymatgen.core.structure import Structure
import re
import logging
from concurrent_log_handler import ConcurrentRotatingFileHandler

from model_client import get_model_client, TimeoutException

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from limits.util import parse_many

logger = logging.getLogger("CrystaLLM")
logger.setLevel(logging.INFO)
file_handler = ConcurrentRotatingFileHandler("app_logs.log", maxBytes=10*1024*1024, backupCount=1000)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def extract_space_group_symbol(cif_str):
    match = re.search(r"_symmetry_space_group_name_H-M\s+('([^']+)'|(\S+))", cif_str)
    if match:
        return match.group(2) if match.group(2) else match.group(3)
    raise Exception(f"could not extract space group from:\n{cif_str}")


def extract_numeric_property(cif_str, prop, numeric_type=float):
    match = re.search(rf"{prop}\s+([.0-9]+)", cif_str)
    if match:
        return numeric_type(match.group(1))
    raise Exception(f"could not find {prop} in:\n{cif_str}")


def get_cell_params(cif_str):
    sg = extract_space_group_symbol(cif_str)
    a = extract_numeric_property(cif_str, "_cell_length_a")
    b = extract_numeric_property(cif_str, "_cell_length_b")
    c = extract_numeric_property(cif_str, "_cell_length_c")
    alpha = extract_numeric_property(cif_str, "_cell_angle_alpha")
    beta = extract_numeric_property(cif_str, "_cell_angle_beta")
    gamma = extract_numeric_property(cif_str, "_cell_angle_gamma")
    return f"{sg} a={a:.3f}Å b={b:.3f}Å c={c:.3f}Å " \
           f"α={alpha:.3f}° β={beta:.3f}° γ={gamma:.3f}°"


def is_non_stoichiometric(composition):
    for amt in composition.values():
        if not amt.is_integer():
            return True
    return False


def load_spacegroups():
    with open("spacegroups.txt", "rt") as f:
        spacegroups = f.read().split("\n")
    return spacegroups


SPACEGROUPS = sorted(load_spacegroups())
FORMULA_UNITS = [1, 2, 3, 4, 6, 8]
client = get_model_client()

external_scripts = [
    "https://ajax.googleapis.com/ajax/libs/jquery/3.6.0/jquery.min.js"
]

external_stylesheets = [
    {
        "href": "https://fonts.googleapis.com/css?family=Droid+Serif|Source+Sans+Pro",
        "rel": "stylesheet"
    }
]

app = dash.Dash(
    __name__,
    prevent_initial_callbacks=False,
    external_scripts=external_scripts,
    external_stylesheets=external_stylesheets,
)
app._favicon = "materialis-icon.png"
app.title = "CrystaLLM"
logger.info(f"using model client: {type(client).__name__}")

server = app.server

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri="redis://localhost:6379/0",
    strategy="moving-window",
    fail_on_first_breach=False,
)

limiter.init_app(server)

RULE_STR = "5/minute;30/hour;100/day"
LIMITS = parse_many(RULE_STR) 


def check_gen_rate_limit() -> bool:
    key = f"gen:{get_remote_address()}"
    for limit in LIMITS:
        if not limiter.limiter.hit(limit, key):
            return False
    return True


structure_component = ctc.StructureMoleculeComponent(
    id="generated_structure",
    show_expand_button=False,
)

app.layout = html.Div([
    html.Div(
        className="header",
        children=[
            html.A(
                href="https://crystallm.com",
                className="homelink",
                children=[
                    html.Img(
                        src="/assets/logo.png",
                        className="logo",
                        width=143,
                        alt="logo"
                    )
                ]
            ),
            html.Div(
                className="header-right",
                children=[
                    html.A(
                        href="https://www.nature.com/articles/s41467-024-54639-7",
                        target="_blank",
                        children=[
                            html.Div("Paper")
                        ]
                    ),
                    html.A(
                        href="https://materialis.ai/contact.html",
                        children=[
                            html.Div("Contact")
                        ]
                    )
                ]
            )
        ]
    ),
    html.Div(
        id="container",
        children=[
            dcc.Store(id="dummy-store", storage_type="session"),
            dcc.Store(id="button-control-store", data=0),
            dcc.Store(id='dummy-output', data=0),
            html.H1(children=[
                "Generate a crystal structure from a composition ",
                html.Span(
                    className="disclaimer",
                    children=["*"]
                )
            ]),
            html.Label("Composition: ", htmlFor="composition"),
            dcc.Input(
                id="composition",
                name="composition",
                type="text",
                placeholder="e.g. PbTe, Bi2Se3",
            ),
            html.Div(id="formula-unit-container", children=[
                dbc.Tooltip(
                    "No. of formula units per cell",
                    target="formula-unit",
                    placement="top",
                ),
                dcc.Dropdown(
                    id="formula-unit",
                    options=FORMULA_UNITS,
                    placeholder="Z",
                ),
                html.Div(id="formula-unit-optional", children=["optional"]),
            ]),
            html.Div(id="spacegroup-container", children=[
                dcc.Dropdown(
                    id="spacegroup",
                    options=SPACEGROUPS,
                    placeholder="Space group",
                ),
                html.Div(id="spacegroup-optional", children=["optional"]),
            ]),
            html.Button(
                "Generate!",
                id="submit-button",
                className="mtrls-button",
                n_clicks=0,
            ),
            html.Details([
                html.Summary("Advanced options", className="advanced-options"),
                html.Label("Model size: ", htmlFor="model-size", className="model-size-label"),
                dcc.Dropdown(
                    id="model-size",
                    options=[
                        {"label": "small", "value": "CrystaLLM"},
                        {"label": "large (may take longer)", "value": "CrystaLLM-Large"},
                    ],
                    value="CrystaLLM",
                    clearable=False,
                    searchable=False,
                    placeholder="Model size",
                ),
            ], className="advanced-details"),
            html.Div(id="generated-structure-container", children=[
                structure_component.layout()
                # we have to display block here, because if none, then we can't rotate structure
            ], style={"display": "block"}),
            html.Div(id="cell-parameters"),
            html.Div(id="message-container", children=[
                html.Div(id="result-container", style={"minHeight": "45px"}),
                html.Div(id="progress", children=[
                    html.Div(className="progress-bar-container", children=[
                        html.Div(className="progress-bar", children=[
                            html.Span(className="progress-bar-inner stripes animated reverse slower")
                        ])
                    ]),
                ], style={"display": "none"}),
            ]),
        ]
    ),
    html.Div(id="model-version", children="CrystaLLM v1.0"),
    html.Footer(
        children=[
            html.P(
                id="disclaimercontent",
                children=[
                    html.Span(
                        style={"fontFamily": "system-ui !important"},
                        children=["*"]
                    ),
                    """
        This service is intended for non-commercial use only. We make no guarantees regarding the accuracy of
        the predictions. The predictions are made by machine learning models that are statistical in nature, and not
        necessarily based on physical theory. If you would like to use this service for commercial purposes, or in a
        high-throughput manner, please """,
                    html.A(
                        href="https://materialis.ai/contact.html",
                        children=["contact us"]
                    ),
                    ". "
                ]
            ),
        html.P(
           className="copyright",
           children=[
            "© Materialis.AI 2025 | ",
            html.A(
            className="termslink",
            style={"color": "#000000"},
            href="https://crystallm.com/privacy.html",
            children=[
                "Privacy Policy"
            ]
        ),
        " | ",
        html.A(
            className="termslink",
            style={"color": "#000000"},
            href="https://crystallm.com/terms.html",
            children=[
                "Terms & Conditions"
            ]
        ),
    ]
)
        ]
    )
])

# This is part of a workaround that enables easy rotation of the structure
#  in the StructureMoleculeComponent canvas on mobile/touchscreen devices.
# The dcc.Store with id 'dummy-store' will trigger this callback on the
#  initial page load and whenever its data changes. So, the dcc.Store is
#  not really being used to store data here. It's just a convenient way to
#  trigger a callback when the app loads, so that we can run some JavaScript code.
app.clientside_callback(
    """
    function(data) {
        dash_clientside.clientside.attachClickHandler(data);
        addTouchHandler();
        return data;
    }
    """,
    Output("dummy-store", "data"),
    Input("dummy-store", "data")
)

app.clientside_callback(
    """
    function(data) {
        return dash_clientside.clientside.setButtonState(data);
    }
    """,
    Output("dummy-output", "data"),
    Input("button-control-store", "data")
)


@app.callback(
    Output("result-container", "children"),
    Output(structure_component.id(), "data"),
    Output("generated-structure-container", "style"),
    Output("cell-parameters", "children"),
    Output("button-control-store", "data"),
    Input("submit-button", "n_clicks"),
    State("composition", "value"),
    State("formula-unit", "value"),
    State("spacegroup", "value"),
    State("model-size", "value")
)
def update_result(n_clicks, composition_value, formula_unit_value, spacegroup_value, model_size_value):
    if n_clicks > 0:

        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        logger.info(f"request made from IP: {ip}")

        if not check_gen_rate_limit():
            logger.warning(f"rate-limit exceeded for {ip}")
            return (
                html.Div(className="error-message",
                         children="You've reached the limit for how many requests can be made in a short time. "
                                  "This helps keep the service responsive for everyone. If you need higher-volume "
                                  "access for research or integration purposes, please contact us."),
                None, {"display": "none"}, None, 0
            )

        if not composition_value:
            logger.error("no composition provided")
            return html.Div(
                className="error-message",
                children="Please enter a composition.",
            ), None, {"display": "none"}, None, 0

        try:
            comp = Composition(composition_value)
        except Exception as e:
            logger.exception(f"error creating composition: {composition_value}; {e}")
            return html.Div(
                className="error-message",
                children="Invalid composition. Please enter a valid composition.",
            ), None, {"display": "none"}, None, 0

        if is_non_stoichiometric(comp):
            msg = f"The composition {composition_value} is non-stoichiometric. " \
                  f"Non-stoichiometric compositions are not supported."
            logger.error(msg)
            return html.Div(
                className="error-message",
                children=msg,
            ), None, {"display": "none"}, None, 0

        if len(comp) == 0:
            msg = f"The composition {composition_value} is empty."
            logger.error(msg)
            return html.Div(
                className="error-message",
                children=msg,
            ), None, {"display": "none"}, None, 0

        reduced_comp, Z = comp.get_reduced_composition_and_factor()

        if Z != 1:
            logger.error("number of formula units is not 1")
            return html.Div(
                className="error-message",
                children="Please enter a reduced formula unit; the current composition has a common divisor in its "
                         "stoichiometric coefficients. To specify a cell composition, use the dropdown to select the "
                         "number of formula units per cell (Z).",
            ), None, {"display": "none"}, None, 0

        comp_str = reduced_comp.to_pretty_string()

        if '+' in comp_str:
            logger.error(f"invalid composition: {comp_str}")
            return html.Div(
                className="error-message",
                children="Invalid composition. Please enter a valid composition.",
            ), None, {"display": "none"}, None, 0

        logger.info(f"processing composition: {comp_str}")

        message = {"comp": comp_str}
        if spacegroup_value:
            message["sg"] = spacegroup_value
        if formula_unit_value:
            message["z"] = formula_unit_value

        logger.info(f"sending message to app {model_size_value}: {message}")

        # TODO display message while working: "The request may take up to 2 minutes to process..."

        try:
            json_resp = client.send(model_size_value, message)
        except TimeoutException as e:
            logger.exception(f"request to model timed out: {e}")
            return html.Div(
                className="error-message",
                children="The generation process exceeded the allotted time and was terminated. "
                         "To reduce the likelihood of a timeout, try specifying a Z value.",
            ), None, {"display": "none"}, None, 0
        except Exception as e:
            logger.exception(f"error making request to model: {e}")
            return html.Div(
                className="error-message",
                children="There was an error sending the request to the model. "
                         "Please try again, or contact us.",
            ), None, {"display": "none"}, None, 0

        if "cifs" not in json_resp:
            logger.error(f'error making request to model: "cifs" not in response: {json_resp}')
            return html.Div(
                className="error-message",
                children="There was an error sending the request to the model. "
                         "Please try again, or contact us.",
            ), None, {"display": "none"}, None, 0

        logger.info(f"response received: {json_resp}")

        cif_response = json_resp["cifs"][0]
        cif_str = cif_response["generated"]
        is_valid = cif_response["valid"]

        if not is_valid:
            logger.info("invalid generation")
            return html.Div(
                className="error-message",
                children=[
                    "Generation failed to produce a valid structure. "
                    "Please try again, or enter a different composition. Failure reasons:",
                    *[html.Div("• " + msg) for msg in cif_response["messages"]],
                    html.Details([
                        html.Summary("View generated CIF", className="cif-summary"),
                        html.Pre(cif_str, className="returned-cif"),
                    ], className="returned-cif-details"),
                ]
            ), None, {"display": "none"}, None, 0

        try:
            structure = Structure.from_str(cif_str, fmt="cif")
            cell_params = get_cell_params(cif_str)
            return html.Div([]), structure, {"display": "block"}, cell_params, 0
        except Exception as e:
            logger.exception(f"there was an error reading the structure: {e}")
            return html.Div(
                className="error-message",
                children=[
                    "Generation failed to produce a valid structure. "
                    "Please try again, or enter a different composition.",
                    html.Details([
                        html.Summary("View generated CIF", className="cif-summary"),
                        html.Pre(cif_str, className="returned-cif"),
                    ], className="returned-cif-details"),
                ]
            ), None, {"display": "none"}, None, 0

    return None, None, {"display": "none"}, None, 0


@app.server.route("/api.html")
def serve_static():
    return send_from_directory("assets", "api.html")


@app.server.route("/privacy.html")
def serve_privacy_policy():
    return send_from_directory("assets", "privacy.html")


@app.server.route("/terms.html")
def serve_terms_and_conditions():
    return send_from_directory("assets", "terms.html")


ctc.register_crystal_toolkit(app=app, layout=app.layout)
if __name__ == "__main__":
    app.run_server(debug=True, port=8050)


