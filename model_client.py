import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests


class ModelClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrystaLLMPiClientConfig:
    api_url: str
    request_timeout_s: int
    poll_timeout_s: int
    poll_interval_s: float

    # Shared host paths (must be bind-mounted into the docker container)
    shared_outputs_dir_host: Path

    # Default models
    model_base: str
    model_pxrd: str


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _build_headers() -> Dict[str, str]:
    """
    Keep future remote-host compatibility:
    - CRYSTALLM_PI_API_KEY -> sent as Authorization: Bearer <key>
    - CRYSTALLM_PI_EXTRA_HEADERS -> JSON string merged in (advanced)
    """
    headers: Dict[str, str] = {"Content-Type": "application/json"}

    api_key = os.getenv("CRYSTALLM_PI_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    extra = os.getenv("CRYSTALLM_PI_EXTRA_HEADERS")
    if extra:
        try:
            headers.update(json.loads(extra))
        except Exception:
            # ignore malformed extra headers
            pass

    return headers


def _find_cif_in_parquet(df) -> str:
    """
    We don't assume a fixed parquet schema.
    Heuristic:
      - prefer columns named exactly CIF/cif
      - else any column containing 'cif' (case-insensitive)
      - else fail with a helpful message
    """
    cols = list(df.columns)
    preferred = [c for c in cols if str(c).lower() == "cif"]
    candidates = preferred or [c for c in cols if "cif" in str(c).lower()]

    if not candidates:
        raise ModelClientError(f"Parquet did not contain a CIF-like column. Columns: {cols}")

    col = candidates[0]
    val = df.iloc[0][col]
    if not isinstance(val, str) or len(val.strip()) < 20:
        raise ModelClientError(f"Found CIF column '{col}' but first row did not look like CIF text.")
    return val


class CrystaLLMPiApiClient:
    """
    Talks to the CrystaLLM-pi containerised API.

    Assumptions for local Docker mode:
      - The API container is running and reachable at CRYSTALLM_PI_API_URL (default http://localhost:8000)
      - Host shared outputs dir is bind-mounted to /app/outputs in the container
      - We request output_parquet under /app/outputs and then read the corresponding host file.
    """

    def __init__(self, cfg: CrystaLLMPiClientConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.headers = _build_headers()

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.cfg.api_url.rstrip("/") + path
        try:
            resp = self.session.post(
                url,
                headers=self.headers,
                data=json.dumps(payload),
                timeout=self.cfg.request_timeout_s,
            )
        except requests.RequestException as e:
            raise ModelClientError(f"Could not reach CrystaLLM-π API at {url}: {e}")

        # try to parse json even on error to get message
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code >= 400:
            raise ModelClientError(
                f"CrystaLLM-π API error ({resp.status_code}) at {url}: {data}"
            )

        return data

    def _get_json(self, path: str) -> Dict[str, Any]:
        url = self.cfg.api_url.rstrip("/") + path
        try:
            resp = self.session.get(url, headers=self.headers, timeout=self.cfg.request_timeout_s)
        except requests.RequestException as e:
            raise ModelClientError(f"Error calling {url}: {e}")

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code >= 400:
            raise ModelClientError(f"CrystaLLM-π API error ({resp.status_code}) at {url}: {data}")
        return data

    def _wait_for_output_parquet(self, host_parquet: Path) -> None:
        deadline = time.time() + self.cfg.poll_timeout_s
        last_size = -1

        while time.time() < deadline:
            if host_parquet.exists():
                try:
                    size = host_parquet.stat().st_size
                    # simple "stabilized" check: non-trivial size
                    if size > 200 and size == last_size:
                        return
                    last_size = size
                except Exception:
                    pass
            time.sleep(self.cfg.poll_interval_s)

        raise ModelClientError(
            f"Timed out waiting for output parquet: {host_parquet}. "
            f"Check the API container logs and confirm /app/outputs is mounted to {self.cfg.shared_outputs_dir_host}."
        )

    def generate_cif(
        self,
        composition_explicit: str,
        spacegroup: Optional[str],
        pxrd_csv_container_path: Optional[str],
        num_return_sequences: int = 1,
    ) -> str:
        """
        Returns one CIF text.

        - composition_explicit must be explicit stoichiometry (Pb1Te1).
        - if pxrd_csv_container_path is provided, we use the PXRD model by default.
        """
        model_id = self.cfg.model_pxrd if pxrd_csv_container_path else self.cfg.model_base

        job_id = uuid.uuid4().hex
        output_parquet_container = f"/app/outputs/{job_id}.parquet"
        output_parquet_host = self.cfg.shared_outputs_dir_host / f"{job_id}.parquet"
        output_parquet_host.parent.mkdir(parents=True, exist_ok=True)

        # Choose model_type explicitly (matches CrystaLLM-π README examples):
        # - base unconditional model: "Base"
        # - COD-XRD model: "Slider"
        model_type = "Slider" if pxrd_csv_container_path else "Base"

        payload: Dict[str, Any] = {
            "hf_model_path": model_id,
            "model_type": model_type,
            "compositions": composition_explicit,
            "num_return_sequences": int(num_return_sequences),
            "output_parquet": output_parquet_container,
            "scoring_mode": str("None"),
        }

        # Prompt detail level
        if spacegroup:
            payload["spacegroups"] = str(spacegroup)
            payload["level"] = "level_4"
        else:
            payload["level"] = "level_2"

        # PXRD support (CrystaLLM-π README uses --xrd_csv_files)
        if pxrd_csv_container_path:
            payload["xrd_csv_files"] = [pxrd_csv_container_path]

        # Kick off generation
        resp = self._post_json("/generate/direct", payload)

        # Some deployments might return a job id and run async
        # (README mentions /jobs endpoints)
        returned_job = resp.get("job_id") or resp.get("id")

        if returned_job:
            # Poll the job endpoint (best-effort), but we *still* mainly rely on output parquet
            deadline = time.time() + self.cfg.poll_timeout_s
            while time.time() < deadline:
                try:
                    st = self._get_json(f"/jobs/{returned_job}")
                    status = (st.get("status") or st.get("state") or "").lower()
                    if status in ("completed", "succeeded", "success", "done"):
                        break
                    if status in ("failed", "error"):
                        raise ModelClientError(f"Job failed: {st}")
                except ModelClientError:
                    raise
                except Exception:
                    # don't fail on flaky job endpoint; rely on parquet wait
                    pass
                time.sleep(self.cfg.poll_interval_s)

        # Local docker mode: wait for parquet file to appear in mounted outputs dir
        self._wait_for_output_parquet(output_parquet_host)

        # Read parquet and extract CIF
        try:
            import pandas as pd  # local import to keep module import light

            df = pd.read_parquet(output_parquet_host)
        except Exception as e:
            raise ModelClientError(f"Failed reading output parquet {output_parquet_host}: {e}")

        cif_text = _find_cif_in_parquet(df)
        return cif_text


def get_model_client() -> CrystaLLMPiApiClient:
    cfg = CrystaLLMPiClientConfig(
        api_url=os.getenv("CRYSTALLM_PI_API_URL", "http://localhost:8000"),
        request_timeout_s=_env_int("CRYSTALLM_PI_REQUEST_TIMEOUT_S", 180),
        poll_timeout_s=_env_int("CRYSTALLM_PI_POLL_TIMEOUT_S", 300),
        poll_interval_s=_env_float("CRYSTALLM_PI_POLL_INTERVAL_S", 1.0),
        shared_outputs_dir_host=Path(
            os.getenv("CRYSTALLM_PI_SHARED_OUTPUTS_DIR", "./outputs")
        ).resolve(),
        model_base=os.getenv("CRYSTALLM_PI_MODEL_BASE", "c-bone/CrystaLLM-pi_base"),
        model_pxrd=os.getenv("CRYSTALLM_PI_MODEL_PXRD", "c-bone/CrystaLLM-pi_COD-XRD"),
    )
    return CrystaLLMPiApiClient(cfg)
