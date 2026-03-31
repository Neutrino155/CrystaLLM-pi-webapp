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
    shared_outputs_dir: Path
    model_base: str
    model_pxrd: str
    enable_postprocess: bool
    postprocess_strict: bool


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


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _build_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}

    api_key = os.getenv("CRYSTALLM_PI_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    extra = os.getenv("CRYSTALLM_PI_EXTRA_HEADERS")
    if extra:
        try:
            headers.update(json.loads(extra))
        except Exception:
            pass

    return headers


def _attach_payload(exc: ModelClientError, payload: Optional[Dict[str, Any]]) -> ModelClientError:
    exc.payload = payload
    return exc


def _find_cif_in_parquet(df) -> str:
    cols = list(df.columns)

    ordered_candidates = []
    exact_priority = [
        "Generated CIF",
        "generated cif",
        "cif",
        "CIF",
    ]
    for name in exact_priority:
        if name in cols and name not in ordered_candidates:
            ordered_candidates.append(name)

    for col in cols:
        if "cif" in str(col).lower() and col not in ordered_candidates:
            ordered_candidates.append(col)

    if not ordered_candidates:
        raise ModelClientError(f"Parquet did not contain a CIF-like column. Columns: {cols}")

    col = ordered_candidates[0]
    val = df.iloc[0][col]
    if not isinstance(val, str) or len(val.strip()) < 20:
        raise ModelClientError(f"Found CIF column '{col}' but first row did not look like CIF text.")
    return val


class CrystaLLMPiApiClient:
    """
    Client for the CrystaLLM-pi API.

    The client submits generation jobs, polls for completion, and reads output
    parquet files from the shared outputs directory.
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

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code >= 400:
            raise _attach_payload(
                ModelClientError(f"CrystaLLM-π API error ({resp.status_code}) at {url}: {data}"),
                data if isinstance(data, dict) else None,
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
            raise _attach_payload(
                ModelClientError(f"CrystaLLM-π API error ({resp.status_code}) at {url}: {data}"),
                data if isinstance(data, dict) else None,
            )
        return data

    def _wait_for_output_parquet(self, parquet_path: Path, deadline: Optional[float] = None) -> None:
        deadline = deadline if deadline is not None else (time.time() + self.cfg.poll_timeout_s)
        last_size = -1

        while time.time() < deadline:
            if parquet_path.exists():
                try:
                    size = parquet_path.stat().st_size
                    if size > 200 and size == last_size:
                        return
                    last_size = size
                except Exception:
                    pass
            time.sleep(self.cfg.poll_interval_s)

        raise ModelClientError(
            f"Timed out waiting for output parquet: {parquet_path}. "
            f"CrystaLLM-pi did not produce a result before the timeout. "
            f"Please try again with less conditions or on a simpler composition. "
            f"If this keeps happening, contact support@psdi.ac.uk."
        )

    def _poll_job(self, job_id: str, deadline: Optional[float] = None) -> Optional[Dict[str, Any]]:
        deadline = deadline if deadline is not None else (time.time() + self.cfg.poll_timeout_s)
        last_status: Optional[Dict[str, Any]] = None

        while time.time() < deadline:
            try:
                status_payload = self._get_json(f"/jobs/{job_id}")
                last_status = status_payload
                status = str(status_payload.get("status") or status_payload.get("state") or "").lower()
                if status in ("completed", "succeeded", "success", "done"):
                    return status_payload
                if status in ("failed", "error"):
                    raise _attach_payload(ModelClientError(f"Job failed: {status_payload}"), status_payload)
            except ModelClientError:
                raise
            except Exception:
                pass

            time.sleep(self.cfg.poll_interval_s)

        raise _attach_payload(
            ModelClientError(f"Timed out waiting for job {job_id}. Last status: {last_status}"),
            last_status,
        )

    def _read_cif_from_parquet(self, parquet_path: Path) -> str:
        try:
            import pandas as pd
            df = pd.read_parquet(parquet_path)
        except Exception as e:
            raise ModelClientError(f"Failed reading output parquet {parquet_path}: {e}")
        return _find_cif_in_parquet(df)

    def postprocess_parquet(self, input_parquet_container: str, output_parquet_container: str, deadline: Optional[float] = None) -> None:
        payload = {
            "input_parquet": input_parquet_container,
            "output_parquet": output_parquet_container,
        }
        resp = self._post_json("/generate/postprocess", payload)
        returned_job = resp.get("job_id") or resp.get("id")
        if returned_job:
            self._poll_job(str(returned_job), deadline=deadline)

    def generate_cif(
        self,
        reduced_formula: str,
        z_value: Optional[str],
        spacegroup: Optional[str],
        pxrd_csv_container_path: Optional[str],
        xrd_wavelength: Optional[float] = None,
        num_return_sequences: int = 1,
        max_return_attempts: int = 5,
    ) -> str:
        use_pxrd = bool(pxrd_csv_container_path)
        model_id = self.cfg.model_pxrd if use_pxrd else self.cfg.model_base
        deadline = time.time() + self.cfg.poll_timeout_s

        request_id = uuid.uuid4().hex
        output_parquet_container = f"/app/outputs/{request_id}.parquet"
        output_parquet_path = self.cfg.shared_outputs_dir / f"{request_id}.parquet"
        output_parquet_path.parent.mkdir(parents=True, exist_ok=True)

        payload: Dict[str, Any] = {
            "hf_model_path": model_id,
            "output_parquet": output_parquet_container,
            "reduced_formula_list": reduced_formula,
            "num_return_sequences": int(num_return_sequences),
            "max_return_attempts": int(max_return_attempts),
            "scoring_mode": "logp",
            "target_valid_cifs": 1,
            "temperature": 1.0,
        }

        if z_value:
            payload["z_list"] = str(int(z_value))
        else:
            payload["search_zs"] = True

        if spacegroup:
            payload["spacegroups"] = str(spacegroup)
            payload["level"] = "level_4"
        else:
            payload["level"] = "level_2"

        if use_pxrd:
            payload["xrd_files"] = [pxrd_csv_container_path]
            if xrd_wavelength is not None:
                payload["xrd_wavelength"] = float(xrd_wavelength)

        resp = self._post_json("/generate/direct", payload)
        returned_job = resp.get("job_id") or resp.get("id")
        if returned_job:
            self._poll_job(str(returned_job), deadline=deadline)

        self._wait_for_output_parquet(output_parquet_path, deadline=deadline)
        cif_raw = self._read_cif_from_parquet(output_parquet_path)

        if self.cfg.enable_postprocess:
            post_id = uuid.uuid4().hex
            post_parquet_container = f"/app/outputs/{post_id}_post.parquet"
            post_parquet_path = self.cfg.shared_outputs_dir / f"{post_id}_post.parquet"
            post_parquet_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.postprocess_parquet(
                    input_parquet_container=output_parquet_container,
                    output_parquet_container=post_parquet_container,
                    deadline=deadline,
                )
                self._wait_for_output_parquet(post_parquet_path, deadline=deadline)
                return self._read_cif_from_parquet(post_parquet_path)
            except Exception as e:
                if self.cfg.postprocess_strict:
                    raise ModelClientError(f"Postprocess failed: {e}")
                return cif_raw

        return cif_raw


def get_model_client() -> CrystaLLMPiApiClient:
    cfg = CrystaLLMPiClientConfig(
        api_url=os.getenv("CRYSTALLM_PI_API_URL", "http://localhost:8000"),
        request_timeout_s=_env_int("CRYSTALLM_PI_REQUEST_TIMEOUT_S", 95),
        poll_timeout_s=_env_int("CRYSTALLM_PI_POLL_TIMEOUT_S", 90),
        poll_interval_s=_env_float("CRYSTALLM_PI_POLL_INTERVAL_S", 1.0),
        shared_outputs_dir=Path(os.getenv("CRYSTALLM_PI_SHARED_OUTPUTS_DIR", "/app/outputs")).resolve(),
        model_base=os.getenv("CRYSTALLM_PI_MODEL_BASE", "c-bone/CrystaLLM-pi_Mattergen-XRD"),
        model_pxrd=os.getenv("CRYSTALLM_PI_MODEL_PXRD", "c-bone/CrystaLLM-pi_Mattergen-XRD"),
        enable_postprocess=_env_bool("CRYSTALLM_PI_ENABLE_POSTPROCESS", True),
        postprocess_strict=_env_bool("CRYSTALLM_PI_POSTPROCESS_STRICT", True),
    )
    return CrystaLLMPiApiClient(cfg)
