import base64
import logging
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
import ast

import dash
from dash import dcc, html
from dash.dependencies import Input, Output, State

import plotly.graph_objects as go
from flask import send_from_directory
from pymatgen.core import Composition
from pymatgen.core.structure import Structure

import crystal_toolkit.components as ctc

from model_client import get_model_client, ModelClientError


# -----------------------------
# Logging
# -----------------------------
logger = logging.getLogger("CrystaLLM-pi-webapp")
logger.setLevel(logging.INFO)

LOG_PATH = os.getenv("APP_LOG_PATH", "app_logs.log")
try:
    from concurrent_log_handler import ConcurrentRotatingFileHandler

    file_handler = ConcurrentRotatingFileHandler(
        LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=50
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
    logger.addHandler(file_handler)
except Exception:
    logging.basicConfig(level=logging.INFO)


# -----------------------------
# Paths / assets helpers
# -----------------------------
APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
DATA_DIR = Path(os.getenv("CRYSTALLM_PI_SHARED_DATA_DIR", str(APP_DIR / "data"))).resolve()
OUTPUTS_DIR = Path(os.getenv("CRYSTALLM_PI_SHARED_OUTPUTS_DIR", str(APP_DIR / "outputs"))).resolve()

UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

def ensure_assets_present():
    """
    Dash only auto-serves files from ./assets.
    If assets exist at repo root, copy missing ones into ./assets on startup.
    """
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    candidates = [
        "logo.png",
        "psdi-logo.svg",
        "main.css",
        "clientside.js",
        "progress.js",
        "touch_handler.js",
    ]

    for name in candidates:
        src = APP_DIR / name
        dst = ASSETS_DIR / name
        try:
            if src.exists() and not dst.exists():
                shutil.copyfile(src, dst)
        except Exception as e:
            logger.warning(f"Failed copying asset {name}: {e}")


ensure_assets_present()


# -----------------------------
# CIF parsing helpers (lightweight)
# -----------------------------
def extract_space_group_symbol(cif_str: str) -> str:
    match = re.search(r"_symmetry_space_group_name_H-M\s+('([^']+)'|(\S+))", cif_str)
    if match:
        return match.group(2) if match.group(2) else match.group(3)
    raise ValueError("Could not extract space group from CIF.")


def extract_numeric_property(cif_str: str, prop: str, numeric_type=float):
    match = re.search(rf"{re.escape(prop)}\s+([.0-9]+)", cif_str)
    if match:
        return numeric_type(match.group(1))
    raise ValueError(f"Could not find {prop} in CIF.")


def get_cell_params(cif_str: str) -> str:
    sg = extract_space_group_symbol(cif_str)
    a = extract_numeric_property(cif_str, "_cell_length_a")
    b = extract_numeric_property(cif_str, "_cell_length_b")
    c = extract_numeric_property(cif_str, "_cell_length_c")
    alpha = extract_numeric_property(cif_str, "_cell_angle_alpha")
    beta = extract_numeric_property(cif_str, "_cell_angle_beta")
    gamma = extract_numeric_property(cif_str, "_cell_angle_gamma")
    return (
        f"{sg} | a={a:.3f} Å  b={b:.3f} Å  c={c:.3f} Å  "
        f"α={alpha:.2f}°  β={beta:.2f}°  γ={gamma:.2f}°"
    )




# -----------------------------
# PXRD helpers (preview plot)
# -----------------------------
def pxrd_preview_from_bytes(raw: bytes, max_points: int = 4000) -> dict:
    """Parse a 2-column PXRD CSV (2θ, intensity) from raw bytes for UI preview."""
    import pandas as pd
    from io import BytesIO

    df = pd.read_csv(BytesIO(raw), header=None)
    if df.shape[1] < 2:
        raise ValueError("CSV must have at least 2 columns: 2theta, intensity")

    # coerce + drop bad rows
    two_theta = pd.to_numeric(df.iloc[:, 0], errors="coerce")
    intensity = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    mask = two_theta.notna() & intensity.notna()
    two_theta = two_theta[mask].astype(float).tolist()
    intensity = intensity[mask].astype(float).tolist()

    if len(two_theta) == 0:
        raise ValueError("No numeric data found in CSV.")

    # sort by 2θ
    pairs = sorted(zip(two_theta, intensity), key=lambda x: x[0])
    two_theta, intensity = zip(*pairs)
    two_theta = list(two_theta)
    intensity = list(intensity)

    # downsample if huge
    n = len(two_theta)
    if n > max_points:
        step = max(1, n // max_points)
        two_theta = two_theta[::step]
        intensity = intensity[::step]

    imax = max(float(x) for x in intensity) if intensity else 1.0
    imax = imax if imax != 0 else 1.0
    intensity_norm = [100.0 * float(x) / imax for x in intensity]

    return {
        "two_theta": two_theta,
        "intensity": intensity,
        "intensity_norm": intensity_norm,
        "n_points": len(two_theta),
        "theta_min": float(min(two_theta)),
        "theta_max": float(max(two_theta)),
        "i_max": float(imax),
    }

# -----------------------------
# Chemistry helpers
# -----------------------------
def is_non_stoichiometric(comp: Composition) -> bool:
    for amt in comp.values():
        if not float(amt).is_integer():
            return True
    return False


def reduced_formula_is_reduced(comp: Composition) -> bool:
    _, factor = comp.get_reduced_composition_and_factor()
    return factor == 1


def composition_to_explicit_stoich(comp: Composition) -> str:
    """
    CrystaLLM-pi expects explicit stoichiometry like Cs1Pb1I3.
    Deterministic alphabetical ordering: ElementSymbol + integer count.
    """
    amounts = comp.get_el_amt_dict()
    parts = []
    for el in sorted(amounts.keys()):
        n = int(round(amounts[el]))
        parts.append(f"{el}{n}")
    return "".join(parts)


def multiply_composition(comp: Composition, z: int) -> Composition:
    if z is None or z == 1:
        return comp
    return Composition({el: amt * int(z) for el, amt in comp.get_el_amt_dict().items()})


# -----------------------------
# Spacegroups
# -----------------------------
def load_spacegroups():
    for p in [APP_DIR / "spacegroups.txt", ASSETS_DIR / "spacegroups.txt"]:
        if p.exists():
            return [x.strip() for x in p.read_text().splitlines() if x.strip()]
    return []


SPACEGROUPS = sorted(load_spacegroups())
FORMULA_UNITS = [1, 2, 3, 4, 6, 8]


# -----------------------------
# Failed job handlers
# -----------------------------
def _safe_parse_job_failed_dict(text: str):
    """
    ModelClientError sometimes comes through as:
      "Job failed: {...python dict repr...}"
    Try to parse that dict. Returns dict or None.
    """
    if not text:
        return None
    m = re.search(r"Job failed:\s*(\{.*\})\s*$", text, flags=re.DOTALL)
    if not m:
        return None
    blob = m.group(1)
    try:
        return ast.literal_eval(blob)
    except Exception:
        return None


def _extract_last_exception_line(err_text: str) -> str:
    """
    Pull the most helpful single-line cause from a long traceback string.
    """
    if not err_text:
        return ""
    # Prefer explicit "*Error: ..." lines near the end.
    lines = [ln.strip() for ln in err_text.splitlines() if ln.strip()]
    error_lines = [ln for ln in lines if re.search(r"\b(Error|Exception):\s", ln)]
    if error_lines:
        return error_lines[-1]
    # Fallback: last non-empty line
    return lines[-1] if lines else ""


def format_model_client_error(e: Exception):
    """
    Returns: (user_facing_component, technical_details_str)
    """
    raw = str(e)
    payload = getattr(e, "payload", None)

    # Try parse payload dict if available or embedded in string.
    if isinstance(payload, dict):
        job = payload
    else:
        job = _safe_parse_job_failed_dict(raw)

    job_id = None
    cmd = None
    err_text = None
    stdout_hint = None

    if isinstance(job, dict):
        job_id = job.get("job_id")
        cmd = job.get("command")
        err_text = job.get("error") or raw
        # Try to extract model name from stdout section if present
        if isinstance(err_text, str) and "Stdout:" in err_text:
            stdout_hint = err_text.split("Stdout:", 1)[1].strip()
    else:
        err_text = raw

    last_line = _extract_last_exception_line(err_text or "")

    # Map common failure patterns -> friendlier copy
    friendly_cause = None
    if "Column 'Generated CIF' not found" in (err_text or "") or "Generated CIF" in (last_line or ""):
        friendly_cause = (
            "The backend did not produce any valid CIF output for this request "
            "(generation returned zero results)."
        )
    elif "Failed after" in (err_text or ""):
        friendly_cause = "The backend failed to generate a valid structure after several attempts."
    else:
        friendly_cause = "The backend job failed while generating the structure."

    # Build user-facing component
    tips = [
        "Try again (sampling can be stochastic).",
        "If you set a space group, try removing it or choosing a different one.",
        "If you uploaded PXRD, try generating once without it (or with fewer/cleaner peaks).",
    ]
    support_line = f"Job ID: {job_id}" if job_id else "Job ID unavailable"

    user_component = html.Div(
        [
            html.Div("Generation failed", className="error-message"),
            html.Div(friendly_cause, className="help-text", style={"marginTop": "6px"}),
            html.Ul([html.Li(t) for t in tips], className="help-text", style={"marginTop": "8px"}),
            html.Details(
                [
                    html.Summary("Technical details", className="cif-summary"),
                    html.Pre(
                        "\n".join(
                            [
                                support_line,
                                f"Cause: {last_line}" if last_line else "Cause: (unknown)",
                                "",
                                "Command:",
                                (cmd or "(not provided)"),
                                "",
                                "Error output (truncated):",
                                (err_text or "")[:4000],
                                "",
                                "Stdout (truncated):",
                                (stdout_hint or "")[:2000],
                            ]
                        ),
                        className="returned-cif",
                        style={"maxHeight": "320px"},
                    ),
                ],
                className="returned-cif-details",
                open=False,
            ),
        ]
    )

    tech_details = {"job_id": job_id, "cause": last_line, "command": cmd}
    return user_component, tech_details


# -----------------------------
# Viewer styles (important)
# -----------------------------
# We DON'T use display:none (it breaks WebGL/VTK init). Instead, keep mounted off-screen.
VIEWER_HIDDEN = {
    "position": "absolute",
    "left": "-10000px",
    "top": "0px",
    "width": "900px",
    "height": "650px",
    "opacity": 0,
    "pointerEvents": "none",
}

VIEWER_SHOWN = {
    "position": "static",
    "left": "auto",
    "top": "auto",
    "width": "auto",
    "height": "auto",
    "opacity": 1,
    "pointerEvents": "auto",
}

# PXRD viewer styles (Plotly)
# We also avoid display:none for the same reason as the WebGL viewer: Plotly can size to 0
# and fail to re-layout when re-shown.
XRD_HIDDEN = {
    "position": "absolute",
    "left": "-10000px",
    "top": "0px",
    "width": "900px",
    "height": "380px",
    "opacity": 0,
    "pointerEvents": "none",
}

XRD_SHOWN = {
    "position": "static",
    "left": "auto",
    "top": "auto",
    "width": "auto",
    "height": "auto",
    "opacity": 1,
    "pointerEvents": "auto",
}

PANEL_HIDDEN = {
    "position": "absolute",
    "left": "-10000px",
    "top": "0px",
    "width": "900px",
    "opacity": 0,
    "pointerEvents": "none",
}

PANEL_SHOWN = {
    "position": "static",
    "left": "auto",
    "top": "auto",
    "width": "auto",
    "opacity": 1,
    "pointerEvents": "auto",
}


# -----------------------------
# Dash app
# -----------------------------
external_scripts = [
    "https://ajax.googleapis.com/ajax/libs/jquery/3.6.0/jquery.min.js",
]
external_stylesheets = [
    {
        "href": "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap",
        "rel": "stylesheet",
    }
]
app = dash.Dash(
    __name__,
    prevent_initial_callbacks=False,
    external_scripts=external_scripts,
    external_stylesheets=external_stylesheets,
    assets_folder=str(ASSETS_DIR),
)
app.title = "CrystaLLM-π Webapp"
app._favicon = "logo.png"
server = app.server  # for gunicorn

client = get_model_client()
logger.info(f"Using model client: {type(client).__name__}")
logger.info(f"DATA_DIR={DATA_DIR} OUTPUTS_DIR={OUTPUTS_DIR}")


# Crystal Toolkit 3D viewer component
structure_component = ctc.StructureMoleculeComponent(
    id="structure-viewer",
    show_expand_button=False,
)


def header():
    return html.Div(
        className="header",
        children=[
            html.A(
                href="#",
                className="homelink",
                children=[
                    html.Img(
                        src="/assets/logo.png",
                        className="logo",
                        width=143,
                        alt="logo",
                    ),
                    html.Div(
                        className="brand-text",
                        children=[
                            html.Div("CrystaLLM-π", className="brand-title"),
                            html.Div("Crystal structure generation", className="brand-subtitle"),
                        ],
                    ),
                ],
            ),
            html.Div(
                className="header-right",
                children=[
                    html.A(
                        href="https://github.com/C-Bone-UCL/CrystaLLM-pi",
                        target="_blank",
                        children=[html.Div("GitHub")],
                    ),
                    html.A(
                        href="https://arxiv.org/abs/2511.21299",
                        children=[html.Div("Paper")],
                    ),
                ],
            ),
        ],
    )


app.layout = html.Div(
    [
        header(),
        html.Div(
            id="container",
            className="container",
            children=[
                dcc.Store(id="dummy-store", storage_type="session"),
                dcc.Store(id="button-control-store", data=0),  # server writes 0 to re-enable
                dcc.Store(
                    id="ui-signal",
                    data=0,  # clientside writes here (avoid duplicate outputs)
                ),
                dcc.Store(
                    id="viewer-resize-ping",
                    data=0,  # clientside "ping"
                ),
                dcc.Store(
                    id="xrd-resize-ping",
                    data=0,  # clientside "ping"
                ),
                dcc.Store(id="progress-active", data=0),
                dcc.Store(id="progress-value", data=0),
                dcc.Store(id="gen-done-signal", data=""),
                html.Div(id="loading-sentinel", style={"display": "none"}),
                dcc.Store(
                    id="pxrd-store",
                    data=None,  # {host_path, container_path, filename}
                ),
                dcc.Store(id="cif-store", data=None),
                html.Div(
                    className="hero",
                    children=[
                        html.H1("Generate a crystal structure", className="hero-title"),
                        html.P(
                            "Create a candidate structure (CIF) from composition, with optional space group and PXRD constraints.",
                            className="hero-subtitle",
                        ),
                    ],
                ),
                html.Div(
                    className="page-grid",
                    children=[
                        html.Div(
                            className="card card--inputs",
                            children=[
                                html.Div(
                                    className="form-row",
                                    children=[
                                        html.Label(
                                            "Composition (reduced formula):",
                                            htmlFor="composition",
                                        ),
                                        dcc.Input(
                                            id="composition",
                                            type="text",
                                            placeholder="e.g. PbTe, Bi2Se3, CsPbI3",
                                            className="text-input",
                                        ),
                                        html.Div(
                                            className="help-text",
                                            children=[
                                                "Tip: You can type reduced formulas (PbTe). We convert to explicit stoichiometry (Pb1Te1) for CrystaLLM-π."
                                            ],
                                        ),
                                    ],
                                ),
                                html.Div(
                                    className="form-row",
                                    children=[
                                        html.Label(
                                            "Z (formula units per cell, optional):",
                                            htmlFor="formula-unit",
                                        ),
                                        dcc.Dropdown(
                                            id="formula-unit",
                                            options=[
                                                {"label": str(z), "value": z}
                                                for z in FORMULA_UNITS
                                            ],
                                            placeholder="Z",
                                            clearable=True,
                                            searchable=False,
                                            className="dropdown",
                                        ),
                                        html.Div(
                                            className="help-text",
                                            children=[
                                                "If provided, we multiply the reduced formula by Z before sending to the model."
                                            ],
                                        ),
                                    ],
                                ),
                                html.Div(
                                    className="form-row",
                                    children=[
                                        html.Label(
                                            "Space group (optional):",
                                            htmlFor="spacegroup",
                                        ),
                                        dcc.Dropdown(
                                            id="spacegroup",
                                            options=[
                                                {"label": sg, "value": sg}
                                                for sg in SPACEGROUPS
                                            ],
                                            placeholder="Space group (e.g., P4_2/mnm)",
                                            clearable=True,
                                            searchable=True,
                                            className="dropdown",
                                        ),
                                    ],
                                ),
                                html.Div(
                                    className="form-row",
                                    children=[
                                        html.Label(
                                            "PXRD CSV (optional):",
                                            htmlFor="pxrd-upload",
                                        ),
                                        dcc.Upload(
                                            id="pxrd-upload",
                                            children=html.Div(
                                                [
                                                    "Drag & drop a .csv here, or ",
                                                    html.A("browse"),
                                                ]
                                            ),
                                            className="upload-box",
                                            multiple=False,
                                        ),
                                        html.Div(
                                            id="pxrd-status",
                                            className="help-text",
                                        ),
                                        html.Div(
                                            className="help-text",
                                            children=[
                                                "CSV format: 2 columns = 2θ (0–90), intensity (0–100), up to ~20 strongest peaks."
                                            ],
                                        ),
                                    ],
                                ),
                                html.Button(
                                    "Generate CIF",
                                    id="submit-button",
                                    className="mtrls-button",
                                    n_clicks=0,
                                ),
                                dcc.Interval(
                                    id="progress-interval",
                                    interval=200,
                                    disabled=True,
                                ),
                                html.Div(
                                    id="progress",
                                    children=[
                                        html.Div(
                                            className="progress-bar-container",
                                            children=[
                                                html.Div(
                                                    className="progress-bar",
                                                    children=[
                                                        html.Span(
                                                            id="progress-bar-inner",
                                                            className="progress-bar-inner",
                                                            style={"width": "0%"},
                                                        )
                                                    ],
                                                )
                                            ],
                                        )
                                    ],
                                    style={"display": "none"},
                                ),
                            ],
                        ),
                        html.Div(
                            className="card card--outputs",
                            children=[
                                # -------------------------
                                # Empty state (default view)
                                # -------------------------
                                html.Div(
                                    id="empty-state",
                                    className="empty-state",
                                    children=[
                                        html.Div(
                                            className="empty-state__inner",
                                            children=[
                                                html.Div(
                                                    className="empty-state__copy",
                                                    children=[
                                                        html.Div("Outputs", className="empty-state__badge"),
                                                        html.H3("Your results will appear here", className="empty-state__title"),
                                                        html.P(
                                                            "Enter a composition and click Generate to see the structure, "
                                                            "and optionally upload a PXRD CSV to preview the pattern.",
                                                            className="empty-state__subtitle",
                                                        ),
                                                        html.Div(
                                                            className="empty-state__steps",
                                                            children=[
                                                                html.Div(
                                                                    className="empty-step",
                                                                    children=[
                                                                        html.Div("1", className="empty-step__num"),
                                                                        html.Div(
                                                                            [
                                                                                html.Div("Add a composition", className="empty-step__label"),
                                                                                html.Div("e.g. TiO2, PbTe, Bi2Se3", className="empty-step__hint"),
                                                                            ],
                                                                            className="empty-step__text",
                                                                        ),
                                                                    ],
                                                                ),
                                                                html.Div(className="empty-state__divider"),
                                                                html.Div(
                                                                    className="empty-step",
                                                                    children=[
                                                                        html.Div("2", className="empty-step__num"),
                                                                        html.Div(
                                                                            [
                                                                                html.Div("Optional constraints", className="empty-step__label"),
                                                                                html.Div("Pick a space group and/or upload PXRD CSV", className="empty-step__hint"),
                                                                            ],
                                                                            className="empty-step__text",
                                                                        ),
                                                                    ],
                                                                ),
                                                                html.Div(className="empty-state__divider"),
                                                                html.Div(
                                                                    className="empty-step",
                                                                    children=[
                                                                        html.Div("3", className="empty-step__num"),
                                                                        html.Div(
                                                                            [
                                                                                html.Div("Generate a CIF", className="empty-step__label"),
                                                                                html.Div("View structure + download CIF", className="empty-step__hint"),
                                                                            ],
                                                                            className="empty-step__text",
                                                                        ),
                                                                    ],
                                                                ),
                                                            ],
                                                        ),
                                                        html.Div(
                                                            className="empty-state__examples",
                                                            children=[
                                                                html.Span("Try:", className="empty-state__examples-label"),
                                                                html.Code("TiO2", className="empty-state__chip"),
                                                                html.Code("CsPbI3", className="empty-state__chip"),
                                                                html.Code("Bi2Se3", className="empty-state__chip"),
                                                            ],
                                                        ),
                                                    ],
                                                ),
                                                html.Div(
                                                    className="empty-state__mock",
                                                    children=[
                                                        html.Div(
                                                            className="mock-card",
                                                            children=[
                                                                html.Div("PXRD preview", className="mock-card__title"),
                                                                html.Div(className="mock-plot"),
                                                            ],
                                                        ),
                                                        html.Div(
                                                            className="mock-card",
                                                            children=[
                                                                html.Div("Structure viewer", className="mock-card__title"),
                                                                html.Div(className="mock-viewer"),
                                                            ],
                                                        ),
                                                    ],
                                                ),
                                            ],
                                        )
                                    ],
                                ),

                                # -------------------------
                                # PXRD-only panel (shows when pxrd-store has data)
                                # -------------------------
                                html.Div(
                                    id="pxrd-panel",
                                    style=PANEL_HIDDEN,
                                    children=[
                                        html.Div(
                                            id="xrd-viewer-container",
                                            className="xrd-viewer-container",
                                            children=[
                                                html.Div(
                                                    className="panel-header",
                                                    children=[
                                                        html.H3("PXRD preview", className="panel-title"),
                                                        html.Div(id="xrd-meta", className="panel-meta"),
                                                    ],
                                                ),
                                                dcc.Graph(
                                                    id="pxrd-graph",
                                                    className="xrd-graph",
                                                    style={"height": "320px"},
                                                    figure=go.Figure(),
                                                    config={
                                                        "displaylogo": False,
                                                        "responsive": True,
                                                    },
                                                ),
                                            ],
                                        ),
                                    ],
                                ),

                                # -------------------------
                                # Generated outputs panel (shows when cif-store has data)
                                # -------------------------
                                html.Div(
                                    id="generated-panel",
                                    style=PANEL_HIDDEN,
                                    children=[
                                        # Always: status / error messages
                                        html.Div(id="result-container", className="result-container"),

                                        # Success-only: only show when cif-store exists
                                        html.Div(
                                            id="success-only",
                                            style=PANEL_HIDDEN,
                                            children=[
                                                html.Div(id="cell-parameters", className="cell-params"),

                                                html.Div(
                                                    id="viewer-container",
                                                    className="viewer-container",
                                                    style=VIEWER_HIDDEN,
                                                    children=[
                                                        html.Div(
                                                            className="panel-header",
                                                            children=[
                                                                html.H3("Structure viewer", className="panel-title"),
                                                                html.Div(
                                                                    "Drag to rotate • Scroll to zoom • Shift+drag to pan",
                                                                    className="panel-meta",
                                                                ),
                                                            ],
                                                        ),
                                                        html.Div(
                                                            className="ctk-viewer-frame",
                                                            children=[structure_component.layout()],
                                                        ),
                                                    ],
                                                ),

                                                html.Div(
                                                    className="download-row",
                                                    children=[
                                                        html.Button(
                                                            "Download CIF",
                                                            id="download-button",
                                                            className="mtrls-button secondary",
                                                            n_clicks=0,
                                                            disabled=True,
                                                        ),
                                                        dcc.Download(id="download-cif"),
                                                    ],
                                                ),

                                                html.Details(
                                                    className="returned-cif-details",
                                                    children=[
                                                        html.Summary("View CIF text", className="cif-summary"),
                                                        html.Pre(id="cif-preview", className="returned-cif"),
                                                    ],
                                                    open=False,
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        ),
        html.Footer(
            className="psdi-footer",
            children=[
                html.Div(
                    className="psdi-footer-inner",
                    children=[
                        html.Div(
                            className="psdi-footer-left",
                            children=[
                                html.Div("In partnership with", className="psdi-footer-kicker"),
                                html.Div(
                                    [
                                        "Physical Sciences Data Infrastructure ",
                                        html.Span("(PSDI)", className="psdi-footer-acronym"),
                                    ],
                                    className="psdi-footer-text",
                                ),
                                html.Div(
                                    f"Copyright © {datetime.now().year} CrystaLLM-π",
                                    className="psdi-footer-copy",
                                ),
                            ],
                        ),
                        html.A(
                            href="https://www.psdi.ac.uk/",
                            target="_blank",
                            className="psdi-footer-link",
                            children=html.Div(className="psdi-footer-mark", title="PSDI"),
                        )
                    ],
                )
            ],
        ),

    ]
)



# -----------------------------
# Client-side helpers
# -----------------------------
# 1) Ensure click handler is attached (disables button, starts progress)
app.clientside_callback(
    """
    function(data) {
        if (dash_clientside && dash_clientside.clientside && dash_clientside.clientside.attachClickHandler) {
            dash_clientside.clientside.attachClickHandler(data);
        }
        return data;
    }
    """,
    Output("dummy-store", "data"),
    Input("dummy-store", "data"),
)

# 2) Re-enable the button when server sets button-control-store back to 0
#    IMPORTANT: output to ui-signal to avoid duplicate output on button-control-store
app.clientside_callback(
    """
    function(data) {
        if (dash_clientside && dash_clientside.clientside && dash_clientside.clientside.setButtonState) {
            dash_clientside.clientside.setButtonState(data);
        }
        return data;
    }
    """,
    Output("ui-signal", "data"),
    Input("button-control-store", "data"),
)


# Progress bar: determinate fill (client-side)
# progress-active: 0=hidden, 1=running, 2=done (shown at 100% until next run)
app.clientside_callback(
    """
    function(n_clicks, done_signal, n_intervals, active, value) {
        const ctx = dash_clientside.callback_context;
        const trig = ctx && ctx.triggered_id;
        const a = (active === undefined || active === null) ? 0 : active;
        const v = (value === undefined || value === null) ? 0 : value;

        if (trig === "submit-button") {
            if (!n_clicks) { return [a, v]; }
            return [1, 0];
        }

        if (trig === "gen-done-signal") {
            if (!done_signal) { return [a, v]; }
            return [2, 100];
        }

        if (trig === "progress-interval") {
            if (a !== 1) { return [a, v]; }
            return [1, Math.min(95, v + 1)];
        }

        return [a, v];
    }
    """,
    Output("progress-active", "data"),
    Output("progress-value", "data"),
    Input("submit-button", "n_clicks"),
    Input("gen-done-signal", "data"),
    Input("progress-interval", "n_intervals"),
    State("progress-active", "data"),
    State("progress-value", "data"),
)

app.clientside_callback(
    """
    function(active) {
        if (active === 1) {
            return [false, {"display": "block"}];
        }
        if (active === 2) {
            return [true, {"display": "block"}];
        }
        return [true, {"display": "none"}];
    }
    """,
    Output("progress-interval", "disabled"),
    Output("progress", "style"),
    Input("progress-active", "data"),
)

app.clientside_callback(
    """
    function(value) {
        const v = (value === undefined || value === null) ? 0 : value;
        return {"width": v + "%"};
    }
    """,
    Output("progress-bar-inner", "style"),
    Input("progress-value", "data"),
)

# 3) When viewer is shown, force a resize so the VTK canvas becomes interactive
app.clientside_callback(
    """
    function(style) {
        if (style && style.opacity === 1) {
            setTimeout(() => window.dispatchEvent(new Event("resize")), 50);
            setTimeout(() => window.dispatchEvent(new Event("resize")), 250);
        }
        return 0;
    }
    """,
    Output("viewer-resize-ping", "data"),
    Input("viewer-container", "style"),
)

# 4) When PXRD preview is shown, force a resize so Plotly can re-layout reliably
app.clientside_callback(
    """
    function(style) {
        if (style && style.opacity === 1) {
            setTimeout(() => window.dispatchEvent(new Event("resize")), 50);
            setTimeout(() => window.dispatchEvent(new Event("resize")), 250);
        }
        return 0;
    }
    """,
    Output("xrd-resize-ping", "data"),
    Input("xrd-viewer-container", "style"),
)


# -----------------------------
# PXRD upload handling
# -----------------------------
@app.callback(
    Output("pxrd-store", "data"),
    Output("pxrd-status", "children"),
    Input("pxrd-upload", "contents"),
    State("pxrd-upload", "filename"),
)
def handle_pxrd_upload(contents, filename):
    if not contents:
        return None, ""

    if not filename or not filename.lower().endswith(".csv"):
        return None, html.Span("Please upload a .csv file.", className="error-message")

    try:
        _header, b64data = contents.split(",", 1)
        raw = base64.b64decode(b64data)
    except Exception as e:
        logger.exception(f"PXRD decode failed: {e}")
        return None, html.Span("Could not decode uploaded file.", className="error-message")

    upload_id = uuid.uuid4().hex
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", filename)
    host_path = UPLOADS_DIR / f"{upload_id}_{safe_name}"
    container_path = f"/app/data/uploads/{host_path.name}"

    try:
        host_path.write_bytes(raw)
    except Exception as e:
        logger.exception(f"PXRD save failed: {e}")
        return None, html.Span("Could not save uploaded file.", className="error-message")

    # Parse once for both UI preview + lightweight validation.
    try:
        preview = pxrd_preview_from_bytes(raw)
    except Exception as e:
        msg = f"Uploaded, but preview/validation warning: {e}"
        logger.info(msg)
        return (
            {"host_path": str(host_path), "container_path": container_path, "filename": filename},
            html.Span(msg, className="warn-message"),
        )

    warnings = []
    try:
        if (preview["theta_min"] < 0) or (preview["theta_max"] > 90):
            warnings.append("2θ values should be within 0–90.")
        # original file format expects 0–100, but we preview even if it isn't.
        raw_intensity = preview.get("intensity", [])
        if raw_intensity:
            if (min(raw_intensity) < 0) or (max(raw_intensity) > 100):
                warnings.append("Intensity values should be within 0–100.")
    except Exception:
        # never block upload on preview metadata issues
        pass

    store = {
        "host_path": str(host_path),
        "container_path": container_path,
        "filename": filename,
        "preview": preview,
    }

    if warnings:
        msg = "Uploaded, but validation warning: " + " ".join(warnings)
        logger.info(msg)
        return store, html.Span(msg, className="warn-message")

    return store, html.Span(f"Uploaded: {filename}", className="ok-message")

# -----------------------------
# PXRD preview plot (if provided)
# -----------------------------
@app.callback(
    Output("xrd-viewer-container", "style"),
    Output("pxrd-graph", "figure"),
    Output("xrd-meta", "children"),
    Input("pxrd-store", "data"),
)
def update_pxrd_preview(pxrd_data):
    def empty_fig(message: str = ""):
        fig = go.Figure()
        fig.update_layout(
            height=260,
            margin=dict(l=10, r=10, t=10, b=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            showlegend=False,
        )
        if message:
            fig.add_annotation(
                x=0.5,
                y=0.5,
                xref="paper",
                yref="paper",
                text=message,
                showarrow=False,
                font=dict(size=13),
            )
        return fig

    if not pxrd_data:
        return XRD_HIDDEN, empty_fig(), ""

    preview = (pxrd_data or {}).get("preview")
    if not preview:
        try:
            host_path = (pxrd_data or {}).get("host_path")
            if host_path:
                raw = Path(host_path).read_bytes()
                preview = pxrd_preview_from_bytes(raw)
        except Exception as e:
            msg = f"Preview unavailable: {e}"
            return XRD_SHOWN, empty_fig(msg), msg

    if not preview:
        msg = "Preview unavailable."
        return XRD_SHOWN, empty_fig(msg), msg

    x = preview.get("two_theta", [])
    y = preview.get("intensity_norm") or preview.get("intensity") or []
    if not x or not y:
        msg = "Preview unavailable."
        return XRD_SHOWN, empty_fig(msg), msg

    fig = go.Figure()

    # Render a typical PXRD "stick" plot: vertical lines at each 2θ value.
    x_sticks = []
    y_sticks = []
    for xi, yi in zip(x, y):
        x_sticks.extend([xi, xi, None])
        y_sticks.extend([0, yi, None])

    fig.add_trace(
        go.Scatter(
            x=x_sticks,
            y=y_sticks,
            mode="lines",
            line=dict(width=2),
            name="Intensity",
            hoverinfo="skip",
            showlegend=False,
        )
    )

    # Invisible markers for per-peak hover.
    fig.add_trace(
        go.Scatter(
            x=x,
            y=y,
            mode="markers",
            marker=dict(size=10, opacity=0),
            hovertemplate="2θ=%{x:.2f}°<br>Intensity=%{y:.2f}<extra></extra>",
            showlegend=False,
        )
    )

    fig.update_layout(
        height=320,
        margin=dict(l=44, r=18, t=10, b=40),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        hovermode="closest",
        showlegend=False,
        xaxis_title="2θ (degrees)",
        yaxis_title="Intensity (a.u.)",
        font=dict(
            family="Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial",
            size=12,
            color="rgba(11, 18, 32, 0.92)",
        ),
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(11, 45, 109, 0.10)", zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor="rgba(11, 45, 109, 0.10)", zeroline=False)

    meta = (
        f"{pxrd_data.get('filename', 'PXRD')} • "
        f"{preview.get('n_points', len(x))} pts • "
        f"2θ {preview.get('theta_min', min(x)):.1f}–{preview.get('theta_max', max(x)):.1f}°"
    )

    return XRD_SHOWN, fig, meta

# -----------------------------
# Generate (single CIF)
# -----------------------------
@app.callback(
    Output("result-container", "children"),
    Output("cell-parameters", "children"),
    Output("cif-store", "data"),
    Output("cif-preview", "children"),
    Output("download-button", "disabled"),
    Output("button-control-store", "data"),
    Output(structure_component.id(), "data"),
    Output("viewer-container", "style"),
    Output("gen-done-signal", "data"),
    Output("loading-sentinel", "children"),
    Input("submit-button", "n_clicks"),
    State("composition", "value"),
    State("formula-unit", "value"),
    State("spacegroup", "value"),
    State("pxrd-store", "data"),
    prevent_initial_call=True,
)
def generate_one_cif(n_clicks, composition_value, z_value, spacegroup_value, pxrd_data):
    # With prevent_initial_call=True this won't run on load, but keep safe.
    if not n_clicks:
        return "", "", None, "", True, 0, None, VIEWER_HIDDEN, str(n_clicks), ""

    def err(msg: str):
        return (
            html.Div(msg, className="error-message"),
            "",
            None,
            "",
            True,
            0,
            None,
            VIEWER_HIDDEN,
            str(n_clicks),
            "",
        )

    if not composition_value or not str(composition_value).strip():
        return err("Please enter a composition.")

    try:
        comp = Composition(str(composition_value).strip())
    except Exception as e:
        logger.exception(f"Invalid composition: {composition_value} ({e})")
        return err("Invalid composition. Please enter a valid chemical formula.")

    if len(comp) == 0 or is_non_stoichiometric(comp):
        return err("Composition must be stoichiometric with integer atom counts.")

    if not reduced_formula_is_reduced(comp):
        return err(
            "Please enter a reduced formula (e.g., PbTe not Pb2Te2). Use Z to specify formula units per cell."
        )

    reduced_comp, _ = comp.get_reduced_composition_and_factor()

    try:
        z_int = int(z_value) if z_value else 1
        if z_int < 1:
            raise ValueError("Z must be >= 1")
    except Exception:
        return err("Z must be a positive integer.")

    cell_comp = multiply_composition(reduced_comp, z_int)
    comp_explicit = composition_to_explicit_stoich(cell_comp)

    use_pxrd = pxrd_data is not None

    try:
        cif_text = client.generate_cif(
            composition_explicit=comp_explicit,
            spacegroup=spacegroup_value,
            pxrd_csv_container_path=(pxrd_data["container_path"] if use_pxrd else None),
            num_return_sequences=1,
        )
    except ModelClientError as e:
        logger.exception(f"Model client error: {e}")
        user_component, _tech = format_model_client_error(e)

        return (
            user_component,
            "",
            None,
            "",
            True,
            0,
            None,
            VIEWER_HIDDEN,
            str(n_clicks),
            "",
        )
    except Exception as e:
        logger.exception(f"Unexpected generation error: {e}")
        return err("Unexpected error while generating. Check app_logs.log for details.")

    # cell params (best-effort)
    cell_params = ""
    try:
        cell_params = get_cell_params(cif_text)
    except Exception:
        cell_params = ""

    # parse structure for viewer (best-effort)
    structure_obj = None
    viewer_style = VIEWER_HIDDEN
    try:
        structure_obj = Structure.from_str(cif_text, fmt="cif")
        viewer_style = VIEWER_SHOWN
    except Exception:
        structure_obj = None
        viewer_style = VIEWER_HIDDEN

    ok_msg = html.Div(
        [
            html.Div("Success: generated 1 CIF.", className="ok-message"),
            html.Div(
                [
                    "Sent to model: ",
                    html.Code(comp_explicit),
                    (" | spacegroup=" + str(spacegroup_value)) if spacegroup_value else "",
                    (" | PXRD=" + pxrd_data["filename"]) if use_pxrd else "",
                ],
                className="help-text",
            ),
        ]
    )

    # button-control-store = 0 => re-enable button (clientside reacts)
    return ok_msg, cell_params, cif_text, cif_text, False, 0, structure_obj, viewer_style, str(n_clicks), ""


# -----------------------------
# Download button
# -----------------------------
@app.callback(
    Output("download-cif", "data"),
    Input("download-button", "n_clicks"),
    State("cif-store", "data"),
    State("composition", "value"),
    prevent_initial_call=True,
)
def download_cif(n_clicks, cif_text, composition_value):
    if not n_clicks or not cif_text:
        return dash.no_update

    base = "generated"
    try:
        if composition_value:
            base = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(composition_value).strip()) or "generated"
    except Exception:
        base = "generated"

    filename = f"{base}.cif"
    return dict(content=cif_text, filename=filename, type="text/plain")

# -----------------------------
# Generation panel placeholder
# -----------------------------
@app.callback(
    Output("empty-state", "style"),
    Output("pxrd-panel", "style"),
    Output("generated-panel", "style"),
    Input("pxrd-store", "data"),
    Input("cif-store", "data"),
    Input("result-container", "children"), 
)
def toggle_outputs(pxrd_data, cif_data, result_children):
    has_pxrd = pxrd_data is not None
    has_cif = bool(cif_data)

    # show generated panel if we have *any* message there (success or error)
    has_result_msg = result_children not in (None, "", [], {})

    empty_style = {"display": "none"} if (has_pxrd or has_cif or has_result_msg) else {"display": "block"}
    pxrd_style = PANEL_SHOWN if has_pxrd else PANEL_HIDDEN
    gen_style = PANEL_SHOWN if (has_cif or has_result_msg) else PANEL_HIDDEN

    return empty_style, pxrd_style, gen_style

@app.callback(
    Output("success-only", "style"),
    Input("cif-store", "data"),
)
def toggle_success_only(cif_data):
    return PANEL_SHOWN if bool(cif_data) else PANEL_HIDDEN

# Enable Crystal Toolkit components
ctc.register_crystal_toolkit(app=app, layout=app.layout)

if __name__ == "__main__":
    app.run_server(debug=False, port=int(os.getenv("PORT", "8050")))
