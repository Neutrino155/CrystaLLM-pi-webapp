import base64
import logging
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
import ast
import warnings

import dash
from dash import dcc, html
from dash.dependencies import Input, Output, State

import plotly.graph_objects as go
from flask import request, send_from_directory
from pymatgen.core import Composition
from pymatgen.core.structure import Structure

import crystal_toolkit.components as ctc
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from limits.util import parse_many
from werkzeug.middleware.proxy_fix import ProxyFix

from model_client import get_model_client, ModelClientError


# -----------------------------
# Logging
# -----------------------------
logger = logging.getLogger("CrystaLLM-pi")
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
    """Ensure required static assets are available from Dash's assets directory."""
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
            if src.exists():
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
ALLOWED_XRD_EXTENSIONS = (".csv", ".xy", ".dat", ".txt")
DEMO_XRD_SOURCE = ASSETS_DIR / "rutile_pxrd.csv"
XRD_WAVELENGTH_OPTIONS = [
    {"label": "Cu Kα (default, 1.5406 Å)", "value": "default"},
    {"label": "Mo Kα (0.71073 Å)", "value": "0.71073"},
    {"label": "Co Kα (1.78897 Å)", "value": "1.78897"},
    {"label": "Custom wavelength…", "value": "custom"},
]


def pxrd_preview_from_bytes(raw: bytes, max_points: int = 4000) -> dict:
    """Parse a two-column peak-picked XRD file for preview.

    Comma-separated and whitespace-delimited text files are supported.
    Non-numeric lines are ignored.
    """
    import pandas as pd
    from io import BytesIO

    df = pd.read_csv(
        BytesIO(raw),
        sep=r"[\s,;	]+",
        engine="python",
        comment="#",
        header=None,
        skip_blank_lines=True,
    )
    if df.shape[1] < 2:
        raise ValueError("The uploaded file must contain at least two columns: 2θ and intensity.")

    two_theta = pd.to_numeric(df.iloc[:, 0], errors="coerce")
    intensity = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    mask = two_theta.notna() & intensity.notna()
    two_theta = two_theta[mask].astype(float).tolist()
    intensity = intensity[mask].astype(float).tolist()

    if len(two_theta) == 0:
        raise ValueError("No numeric 2θ/intensity pairs were found in the uploaded file.")

    pairs = sorted(zip(two_theta, intensity), key=lambda x: x[0])
    two_theta, intensity = zip(*pairs)
    two_theta = list(two_theta)
    intensity = list(intensity)

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


def parse_xrd_wavelength(selection: str, custom_value) -> float | None:
    """Return the selected X-ray wavelength in angstroms."""
    selection = (selection or "default").strip()
    if selection in ("", "default"):
        return None
    if selection == "custom":
        if custom_value in (None, ""):
            raise ValueError("Enter a wavelength in angstrom for the custom selection.")
        value = float(custom_value)
    else:
        value = float(selection)

    if value <= 0:
        raise ValueError("The X-ray wavelength must be greater than zero.")
    return value


def save_xrd_file(raw: bytes, filename: str) -> tuple[dict, list[str]]:
    """Save a diffraction file to shared storage and prepare preview metadata."""
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", filename)
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    host_path = UPLOADS_DIR / stored_name
    container_path = f"/app/data/uploads/{stored_name}"

    preview = None
    warnings: list[str] = []
    try:
        preview = pxrd_preview_from_bytes(raw)
        try:
            if (preview["theta_min"] < 0) or (preview["theta_max"] > 90):
                warnings.append("2θ values are usually expected to fall within 0–90° for this interface.")
            raw_intensity = preview.get("intensity", [])
            if raw_intensity and ((min(raw_intensity) < 0) or (max(raw_intensity) > 100)):
                warnings.append("Intensity values are typically expected to be non-negative and commonly normalized.")
        except Exception:
            pass
    except Exception as e:
        warnings.append(f"Preview/validation warning: {e}")

    host_path.write_bytes(raw)

    return ({
        "filename": safe_name,
        "stored_name": stored_name,
        "host_path": str(host_path),
        "container_path": container_path,
        "preview": preview,
    }, warnings)


def make_pxrd_upload():
    return dcc.Upload(
        id="pxrd-upload",
        children=html.Div(["Drag and drop a peak-picked XRD file here, or ", html.A("browse")]),
        className="upload-box",
        multiple=False,
        accept=",".join(ALLOWED_XRD_EXTENSIONS),
    )

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
    """Return an explicit-stoichiometry formula string in deterministic alphabetical order."""
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




def _sanitize_technical_text(text: str) -> str:
    if not text:
        return ""

    cleaned = str(text)
    cleaned = re.sub(r"/app/outputs/[^\s\n'\"]+", "<generated output file>", cleaned)
    cleaned = re.sub(
        r"Confirm the webapp and API both mount the same shared outputs directory at /app/outputs\.?",
        "CrystaLLM-pi did not produce a result before the timeout. ",
        "Please try again with less conditions or on a simpler composition. ",
        "If this keeps happening, contact support@psdi.ac.uk.",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", lambda m: "\n" if "\n" in m.group(0) else " ", cleaned)
    return cleaned.strip()

def format_model_client_error(e: Exception):
    """
    Returns: (user_facing_component, technical_details_dict)
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
        if isinstance(err_text, str) and "Stdout:" in err_text:
            stdout_hint = err_text.split("Stdout:", 1)[1].strip()
    else:
        err_text = raw

    err_text = _sanitize_technical_text(err_text or "")
    cmd = _sanitize_technical_text(cmd or "")
    stdout_hint = _sanitize_technical_text(stdout_hint or "")
    last_line = _sanitize_technical_text(_extract_last_exception_line(err_text or ""))

    # Map common failure patterns -> friendlier copy
    tips = [
        "Try again.",
        "Try removing or changing the space group.",
        "If you uploaded XRD, try again without it or with a cleaner peak list.",
    ]

    if ("FileNotFoundError" in (err_text or "")) or ("No such file or directory" in (err_text or "")):
        friendly_cause = (
            "The uploaded diffraction file could not be located during processing. "
            "Please upload the file again and retry."
        )
        tips = [
            "Re-upload the XRD file and try again.",
            "Try again without XRD to isolate the issue.",
        ] + tips
    elif "Timed out waiting for output parquet" in (err_text or "") or "Timed out waiting for job" in (err_text or ""):
        friendly_cause = "Generation timed out before a CIF could be returned."
        tips = [
            "Try again.",
            "Try changing or removing the space group.",
            "Try again without XRD or with fewer constraints.",
            "If this keeps happening, contact support@psdi.ac.uk.",
        ]
    elif "Column 'Generated CIF' not found" in (err_text or "") or "Generated CIF" in (last_line or ""):
        friendly_cause = (
            "No valid CIF could be produced for this request."
        )
    elif "Failed after" in (err_text or ""):
        friendly_cause = "A valid structure could not be generated after several attempts."
    else:
        friendly_cause = "The generation request did not complete successfully."

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
                        style={
                            "maxHeight": "320px",
                            "whiteSpace": "pre-wrap",
                            "overflowWrap": "anywhere",
                            "wordBreak": "break-word",
                        },
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
app.title = "CrystaLLM-π"
app._favicon = "logo.png"
server = app.server  # for gunicorn

@server.route("/usage")
def usage_guide():
    """Serve the standalone usage guide."""
    return send_from_directory(str(ASSETS_DIR), "usage.html")

# Trust one reverse-proxy hop for client IP discovery (adjust if needed).
server.wsgi_app = ProxyFix(server.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

RATE_LIMIT_STORAGE_URI = os.getenv("RATE_LIMIT_STORAGE_URI", "redis://localhost:6379/0")
RATE_LIMIT_RULE = os.getenv("RATE_LIMIT_RULE", "5/minute;30/hour;100/day")

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=RATE_LIMIT_STORAGE_URI,
    strategy="moving-window",
    fail_on_first_breach=False,
)
limiter.init_app(server)
LIMITS = parse_many(RATE_LIMIT_RULE)


def check_gen_rate_limit() -> bool:
    key = f"gen:{get_remote_address()}"
    for limit in LIMITS:
        if not limiter.limiter.hit(limit, key):
            return False
    return True


client = get_model_client()
logger.info(f"Using model client: {type(client).__name__}")
logger.info("Using shared storage for uploaded diffraction files and generation results")


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
                        href="/usage",
                        children=[html.Div("Guide")],
                    ),
                    html.A(
                        href="https://github.com/C-Bone-UCL/CrystaLLM-pi",
                        target="_blank",
                        children=[html.Div("GitHub")],
                    ),
                    html.A(
                        href="https://arxiv.org/abs/2511.21299",
                        target="_blank",
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
                dcc.Store(id="button-control-store", data=0),  # Server-side flag used to re-enable the submit button.
                dcc.Store(
                    id="ui-signal",
                    data=0,  # Client-side signal used to avoid duplicate outputs.
                ),
                dcc.Store(
                    id="viewer-resize-ping",
                    data=0,  # Client-side resize signal.
                ),
                dcc.Store(
                    id="xrd-resize-ping",
                    data=0,  # Client-side resize signal.
                ),
                dcc.Store(id="gen-done-signal", data=""),
                html.Div(id="loading-sentinel", style={"display": "none"}),
                dcc.Store(
                    id="pxrd-store",
                    data=None,  # Stored upload metadata for the selected diffraction file.
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
                                                "Use a reduced formula, for example PbTe."
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
                                                "Leave blank to infer Z automatically."
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
                                            "Peak-picked XRD file (optional):",
                                            htmlFor="pxrd-upload",
                                        ),
                                        html.Div(
                                            id="pxrd-upload-wrapper",
                                            children=[make_pxrd_upload()],
                                        ),
                                        html.Div(
                                            className="pxrd-status-row",
                                            children=[
                                                html.Div(id="pxrd-status", className="pxrd-status"),
                                                html.Button(
                                                    "×",
                                                    id="pxrd-clear",
                                                    n_clicks=0,
                                                    className="pxrd-clear-btn",
                                                    title="Remove file",
                                                    style={"display": "none"},
                                                ),
                                            ],
                                        ),
                                        html.Div(
                                            className="help-text",
                                            children=[
                                                "Accepted formats: .csv, .xy, .dat, .txt. Upload a peak-picked 2θ/intensity pattern."
                                            ],
                                        ),
                                        html.Button(
                                            "Advanced XRD options",
                                            id="xrd-advanced-toggle",
                                            n_clicks=0,
                                            className="mtrls-button secondary xrd-advanced-toggle",
                                            type="button",
                                        ),
                                        html.Div(
                                            id="xrd-advanced-panel",
                                            className="xrd-advanced-panel",
                                            style={"display": "none"},
                                            children=[
                                                html.Label(
                                                    "X-ray wavelength (optional):",
                                                    htmlFor="xrd-wavelength",
                                                ),
                                                dcc.Dropdown(
                                                    id="xrd-wavelength",
                                                    options=XRD_WAVELENGTH_OPTIONS,
                                                    value="default",
                                                    clearable=False,
                                                    searchable=False,
                                                    className="dropdown",
                                                ),
                                                html.Div(
                                                    id="xrd-wavelength-custom-wrap",
                                                    style={"display": "none", "marginTop": "10px"},
                                                    children=[
                                                        dcc.Input(
                                                            id="xrd-wavelength-custom",
                                                            type="number",
                                                            min=0,
                                                            step="any",
                                                            placeholder="Custom wavelength (Å)",
                                                            className="text-input",
                                                        )
                                                    ],
                                                ),
                                                html.Div(
                                                    className="help-text",
                                                    children=[
                                                        "Default: Cu Kα (1.5406 Å)."
                                                    ],
                                                ),
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
                                    interval=100,
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
                                        ),
                                        html.Div(
                                            className="progress-meta",
                                            children=[
                                                html.Span("Preparing request…", id="progress-stage", className="progress-stage"),
                                                html.Span("0%", id="progress-percent", className="progress-percent"),
                                            ],
                                        ),
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
                                                            "and optionally upload a peak-picked XRD file to preview the pattern.",
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
                                                                                html.Div("Pick a space group and/or upload a peak-picked XRD file", className="empty-step__hint"),
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
                                                            className="empty-state__examples-bar",
                                                            children=[
                                                                html.Div(
                                                                    className="empty-state__examples",
                                                                    children=[
                                                                        html.Span("Try:", className="empty-state__examples-label"),
                                                                        html.Button(
                                                                            "CsPbI3",
                                                                            id="quick-fill-cspbi3",
                                                                            n_clicks=0,
                                                                            className="empty-state__chip-button",
                                                                            type="button",
                                                                        ),
                                                                        html.Button(
                                                                            "TiO2 rutile",
                                                                            id="quick-demo-rutile",
                                                                            n_clicks=0,
                                                                            className="empty-state__chip-button empty-state__chip-button--demo",
                                                                            type="button",
                                                                        ),
                                                                    ],
                                                                ),
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
                                html.Div(
                                    [
                                        "Support: ",
                                        html.A("support@psdi.ac.uk", href="mailto:support@psdi.ac.uk", className="psdi-footer-email"),
                                    ],
                                    className="psdi-footer-support",
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
# 1) Attach the client-side click handler that disables the button and starts progress.
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

# 2) Re-enable the button when the server resets button-control-store to 0.
#    The ui-signal store prevents duplicate-output conflicts on button-control-store.
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


# 3) Keep the old Dash interval disabled; progress is driven directly in browser JS
#    so it starts immediately on click and stops as soon as the response is applied.
app.clientside_callback(
    """
    function(n_clicks, done_signal, n_intervals) {
        return true;
    }
    """,
    Output("progress-interval", "disabled"),
    Input("submit-button", "n_clicks"),
    Input("gen-done-signal", "data"),
    Input("progress-interval", "n_intervals"),
)

# 4) Trigger a resize event when the structure viewer becomes visible.
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

# Clear stale scene selection whenever CTK rebuilds the scene data
app.clientside_callback(
    """
    function(sceneData) {
        if (sceneData === undefined) {
            return window.dash_clientside.no_update;
        }
        return null;
    }
    """,
    Output(structure_component.id("scene"), "selectedObject"),
    Input(structure_component.id("scene"), "data"),
    prevent_initial_call=True,
)

# 4) Trigger a resize event when the PXRD preview becomes visible.
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
    Output("pxrd-clear", "style"),
    Output("pxrd-upload-wrapper", "children"),
    Input("pxrd-upload", "contents"),
    Input("pxrd-clear", "n_clicks"),
    Input("quick-demo-rutile", "n_clicks"),
    State("pxrd-upload", "filename"),
    State("pxrd-store", "data"),
    prevent_initial_call=True,
)
def handle_pxrd_upload(contents, clear_clicks, rutile_demo_clicks, filename, current_store):
    trig = getattr(dash, "ctx", None).triggered_id if hasattr(dash, "ctx") else None

    # Remove the currently selected diffraction file.
    if trig == "pxrd-clear":
        try:
            if current_store and current_store.get("host_path"):
                Path(current_store["host_path"]).unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed removing cleared PXRD file: {e}")
        return (
            None,                  # pxrd-store
            "",                    # pxrd-status
            {"display": "none"},   # hide ×
            [make_pxrd_upload()],  # remount upload (lets user re-upload same file)
        )

    # Replace any previously stored file before saving a new selection.
    if current_store and current_store.get("host_path"):
        try:
            Path(current_store["host_path"]).unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed removing previous PXRD file: {e}")

    if trig == "quick-demo-rutile":
        try:
            raw = DEMO_XRD_SOURCE.read_bytes()
            store, warnings = save_xrd_file(raw, "rutile_pxrd.csv")
        except Exception as e:
            logger.exception(f"Failed preparing demo PXRD file: {e}")
            return None, html.Span("Could not load the demo XRD file.", className="error-message"), {"display": "none"}, dash.no_update

        status_children = [
            html.Div("Demo loaded: TiO2 rutile", className="pxrd-pill pxrd-pill--ok"),
            html.Div("TiO2 has been selected automatically.", className="help-text", style={"marginTop": "6px"}),
        ]
        if warnings:
            status_children.append(html.Div(" ".join(warnings), className="help-text", style={"marginTop": "6px"}))

        return (
            store,
            status_children,
            {"display": "inline-flex"},
            dash.no_update,
        )

    # Handle a newly uploaded diffraction file.
    if not contents:
        return None, "", {"display": "none"}, dash.no_update

    if not filename or not filename.lower().endswith(ALLOWED_XRD_EXTENSIONS):
        return None, html.Span("Please upload a .csv, .xy, .dat, or .txt file.", className="error-message"), {"display": "none"}, dash.no_update

    try:
        _header, b64data = contents.split(",", 1)
        raw = base64.b64decode(b64data)
    except Exception as e:
        logger.exception(f"PXRD decode failed: {e}")
        return None, html.Span("Could not decode uploaded file.", className="error-message"), {"display": "none"}, dash.no_update

    try:
        store, warnings = save_xrd_file(raw, filename)
    except Exception as e:
        logger.exception(f"Failed saving PXRD upload to shared storage: {e}")
        return None, html.Span(
            "Could not save the uploaded XRD file into the shared data directory. "
            "Check that /app/data/uploads exists and is writable.",
            className="error-message",
        ), {"display": "none"}, dash.no_update

    badge_kind = "warn" if warnings else "ok"
    status_children = [
        html.Div(f"Uploaded: {filename}", className=f"pxrd-pill pxrd-pill--{badge_kind}"),
    ]
    if warnings:
        status_children.append(html.Div(" ".join(warnings), className="help-text", style={"marginTop": "6px"}))

    return (
        store,
        status_children,
        {"display": "inline-flex"},  # show ×
        dash.no_update,
    )

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
            content_b64 = (pxrd_data or {}).get("content_base64")
            if content_b64:
                raw = base64.b64decode(content_b64)
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

    # Render a standard stick plot with vertical lines at each 2θ position.
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

    # Invisible markers provide per-peak hover labels.
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

@app.callback(
    Output("xrd-advanced-panel", "style"),
    Output("xrd-advanced-toggle", "children"),
    Input("xrd-advanced-toggle", "n_clicks"),
)
def toggle_xrd_advanced_panel(n_clicks):
    """Toggle the optional XRD controls."""
    is_open = bool(n_clicks and n_clicks % 2 == 1)
    panel_style = {"display": "grid", "gap": "10px", "marginTop": "10px"} if is_open else {"display": "none"}
    button_label = "Advanced XRD options ▴" if is_open else "Advanced XRD options ▾"
    return panel_style, button_label


@app.callback(
    Output("xrd-wavelength-custom-wrap", "style"),
    Input("xrd-wavelength", "value"),
)
def toggle_xrd_wavelength_custom(selection):
    if selection == "custom":
        return {"display": "block", "marginTop": "10px"}
    return {"display": "none", "marginTop": "10px"}


@app.callback(
    Output("composition", "value"),
    Input("quick-fill-cspbi3", "n_clicks"),
    Input("quick-demo-rutile", "n_clicks"),
    State("composition", "value"),
    prevent_initial_call=True,
)
def set_composition_from_examples(cspbi3_clicks, rutile_demo_clicks, current_value):
    trig = getattr(dash, "ctx", None).triggered_id if hasattr(dash, "ctx") else None

    if trig == "quick-fill-cspbi3":
        return "CsPbI3"
    if trig == "quick-demo-rutile":
        return "TiO2"
    return current_value


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
    State("xrd-wavelength", "value"),
    State("xrd-wavelength-custom", "value"),
    prevent_initial_call=True,
)
def generate_one_cif(n_clicks, composition_value, z_value, spacegroup_value, pxrd_data, xrd_wavelength_selection, xrd_wavelength_custom):
    if not n_clicks:
        return "", "", None, "", True, 0, None, VIEWER_HIDDEN, str(n_clicks), ""

    def err(msg: str):
        return (
            html.Div(msg, className="error-message"),  # result-container
            "",                                        # cell-parameters
            None,                                      # cif-store (clears success-only area)
            "",                                        # cif-preview
            True,                                      # download disabled
            0,                                         # re-enable button
            None,                                      # viewer data
            VIEWER_HIDDEN,                             # viewer style
            str(n_clicks),                             # gen-done-signal
            "",                                        # loading-sentinel
        )

    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    logger.info(f"Generation request from IP: {ip}")

    if not check_gen_rate_limit():
        logger.warning(f"Rate limit exceeded for {ip}")
        return err(
            "You've reached the limit for how many generation requests can be made in a short time. "
            "This helps keep the service responsive for everyone. "
            "If you need higher-volume access for research or integration purposes, please contact us."
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
        return err("Please enter a reduced formula (e.g., PbTe not Pb2Te2). Use Z to specify formula units per cell.")

    reduced_comp, _ = comp.get_reduced_composition_and_factor()
    reduced_formula = str(reduced_comp.reduced_formula).replace(" ", "")

    z_to_send = None
    if z_value not in (None, ""):
        try:
            z_int = int(z_value)
            if z_int < 1:
                raise ValueError("Z must be >= 1")
            z_to_send = str(z_int)
        except Exception:
            return err("Z must be a positive integer.")

    use_pxrd = pxrd_data is not None

    xrd_wavelength = None
    if use_pxrd:
        try:
            xrd_wavelength = parse_xrd_wavelength(xrd_wavelength_selection, xrd_wavelength_custom)
        except Exception as e:
            return err(str(e))

    try:
        cif_text = client.generate_cif(
            reduced_formula=reduced_formula,
            z_value=z_to_send,
            spacegroup=spacegroup_value,
            pxrd_csv_container_path=(pxrd_data["container_path"] if use_pxrd else None),
            xrd_wavelength=xrd_wavelength,
            num_return_sequences=1,
        )
    except ModelClientError as e:
        logger.exception(f"Model client error: {e}")
        user_component, _tech = format_model_client_error(e)

        return (
            user_component,   # result-container
            "",               # cell-parameters
            None,             # cif-store
            "",               # cif-preview
            True,             # download disabled
            0,                # re-enable button
            None,             # viewer data
            VIEWER_HIDDEN,    # viewer style
            str(n_clicks),    # gen-done-signal
            "",               # loading-sentinel
        )
    except Exception as e:
        logger.exception(f"Unexpected generation error: {e}")
        return err("Unexpected error while generating. Please refresh and try again.")

    if not cif_text or not str(cif_text).strip():
        logger.warning("Generation completed without returning CIF text.")
        return err("Failed to generate a crystal structure for this input. Please try different constraints and try again.")

    # Extract cell parameters for display when available.
    cell_params = ""
    try:
        cell_params = get_cell_params(cif_text)
    except Exception:
        cell_params = ""

    # Parse the generated structure for the viewer while collecting warnings.
    structure_obj = None
    viewer_style = VIEWER_HIDDEN
    parse_exc = None
    warn_msgs = []

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            structure_obj = Structure.from_str(cif_text, fmt="cif")
        except Exception as e:
            parse_exc = e
            structure_obj = None
        warn_msgs = [str(x.message) for x in (w or []) if str(getattr(x, "message", "")).strip()]

    if structure_obj is not None and parse_exc is None:
        viewer_style = VIEWER_SHOWN

    ok_msg = html.Div(
        [
            html.Div("CIF generated successfully.", className="ok-message"),
            html.Div(
                [
                    "Request summary: ",
                    html.Code(reduced_formula),
                    (" | Z=" + z_to_send) if z_to_send else " | Z=auto",
                    (" | spacegroup=" + str(spacegroup_value)) if spacegroup_value else "",
                    (" | XRD=" + pxrd_data["filename"]) if use_pxrd else "",
                    (f" | λ={xrd_wavelength:.5f} Å" if xrd_wavelength else " | λ=Cu Kα") if use_pxrd else "",
                ],
                className="help-text",
            ),
        ]
    )

    # If parsing fails, show a warning and keep the CIF text available.
    cell_params_children = cell_params
    if viewer_style == VIEWER_HIDDEN:
        cause_line = _extract_last_exception_line(str(parse_exc)) if parse_exc else ""
        warning_pre = "\n".join(
            [
                "Cause:",
                (cause_line or "(unknown)"),
                "",
                "Warnings (if any):",
                ("\n".join(f"- {m}" for m in warn_msgs) if warn_msgs else "(none captured)"),
                "",
                "Note: You can still download the CIF below and inspect the CIF text.",
            ]
        )

        warning_box = html.Div(
            [
                html.Div("Structure viewer unavailable", className="warn-message"),
                html.Div(
                    "A CIF was generated, but it could not be parsed into a valid structure for rendering.",
                    className="help-text",
                    style={"marginTop": "6px"},
                ),
                html.Details(
                    [
                        html.Summary("Technical details", className="cif-summary"),
                        html.Pre(
                            warning_pre,
                            className="returned-cif",
                            style={
                                "maxHeight": "320px",
                                "whiteSpace": "pre-wrap",
                                "overflowWrap": "anywhere",
                                "wordBreak": "break-word",
                            },
                        ),
                    ],
                    className="returned-cif-details",
                    open=False,
                ),
            ],
            style={"marginTop": "10px"},
        )

        # Put warning just under the cell-params line (viewer itself stays hidden)
        if cell_params:
            cell_params_children = html.Div(
                [
                    html.Div(cell_params, className="cell-params"),
                    warning_box,
                ]
            )
        else:
            cell_params_children = warning_box

    return (
        ok_msg,               # result-container
        cell_params_children, # cell-parameters (may include warning box)
        cif_text,             # cif-store => keeps download + CIF text available
        cif_text,             # cif-preview
        False,                # download enabled
        0,                    # re-enable button
        structure_obj,        # viewer data (None if parse failed)
        viewer_style,         # viewer style
        str(n_clicks),        # gen-done-signal
        "",                   # loading-sentinel
    )


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
    Input("submit-button", "n_clicks"),
    Input("gen-done-signal", "data"),
)
def toggle_outputs(pxrd_data, cif_data, result_children, n_clicks, done_signal):
    has_pxrd = pxrd_data is not None
    has_cif = bool(cif_data)
    has_result_msg = result_children not in (None, "", [], {})

    pending = bool(n_clicks) and str(done_signal or "") != str(n_clicks)
    if pending:
        return {"display": "block"}, PANEL_HIDDEN, PANEL_HIDDEN

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
