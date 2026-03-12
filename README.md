# CrystaLLM-pi Webapp

A Dash-based web interface for generating crystal structures with the CrystaLLM-pi API.

The application provides a browser UI for:
- entering a composition, optional `Z`, and optional space group
- uploading a peak-picked X-ray diffraction file for conditioned generation
- specifying an optional X-ray wavelength when the data were collected with a source other than Cu Kα
- loading a bundled TiO2 rutile demo pattern for quick testing
- submitting generation jobs to a remote CrystaLLM-pi API
- visualising the generated structure and downloading the resulting CIF

## Overview

The webapp is intended to run alongside a separately deployed CrystaLLM-pi API service.

A typical deployment has:
- a **GPU host** running the CrystaLLM-pi API on port `8000`
- a **webapp host** running this web application on port `8050`
- a **shared filesystem** that is accessible from both hosts

The shared filesystem is used for:
- uploaded diffraction files
- generated parquet outputs

Inside both containers, the shared locations must be mounted as:
- `/app/data`
- `/app/outputs`

## Requirements

- Docker and Docker Compose on the webapp host
- A running CrystaLLM-pi API deployment reachable over HTTP
- Shared POSIX storage visible to both the API host and the webapp host
- Read and write permissions for the shared data and outputs directories

## Shared storage layout

Create the following directories on the shared filesystem:

```text
/shared/crystallm/data/uploads
/shared/crystallm/outputs
```

You may use different host paths if needed. What matters is that:
- both hosts can access the same underlying storage
- the webapp container mounts the shared data directory to `/app/data`
- the webapp container mounts the shared outputs directory to `/app/outputs`
- the API container does the same

## Configuration

Copy the example environment file:

```bash
cp .env.docker.example .env.docker
```

Edit `.env.docker` and set:

```env
CRYSTALLM_PI_API_URL=http://<api-host>:8000
WEBAPP_SHARED_DATA_HOST_DIR=/shared/crystallm/data
WEBAPP_SHARED_OUTPUTS_HOST_DIR=/shared/crystallm/outputs
RATE_LIMIT_RULE=5/minute;30/hour;100/day
```

### Environment variables

#### `CRYSTALLM_PI_API_URL`
Base URL for the CrystaLLM-pi API.

Example:

```env
CRYSTALLM_PI_API_URL=http://gpu-node.example.org:8000
```

#### `WEBAPP_SHARED_DATA_HOST_DIR`
Host path to the shared data directory on the webapp host.

This directory is mounted into the container as `/app/data`.

#### `WEBAPP_SHARED_OUTPUTS_HOST_DIR`
Host path to the shared outputs directory on the webapp host.

This directory is mounted into the container as `/app/outputs`.

#### `RATE_LIMIT_RULE`
Redis-backed IP rate limit configuration for generation requests.

Example:

```env
RATE_LIMIT_RULE=5/minute;30/hour;100/day
```

## Build

Build the webapp image:

```bash
docker compose --env-file .env.docker build
```

## Run

Start the webapp and Redis:

```bash
docker compose --env-file .env.docker up
```

Run in the background:

```bash
docker compose --env-file .env.docker up -d
```

The web interface will be available at:

```text
http://localhost:8050
```

A standalone usage guide is available at:

```text
http://localhost:8050/usage
```

## Services

The Compose stack includes:
- `webapp`: the Dash application served with Gunicorn
- `redis`: Redis storage for IP-based rate limiting

## Inputs

### Composition
Enter a reduced formula such as `PbTe`, `TiO2`, or `Bi2Se3`.

### Z
Provide `Z` when the number of formula units per unit cell is known. Leave it blank to allow automatic selection during generation.

### Space group
An optional crystallographic space group may be supplied to further constrain generation.

### X-ray diffraction file
The webapp accepts the following file types:
- `.csv`
- `.xy`
- `.dat`
- `.txt`

Upload a **peak-picked two-column pattern** containing:
- `2θ`
- intensity

The web interface does not perform raw diffraction data processing.

A bundled demo pattern is available in the XRD section for the rutile TiO2 example.

### Demo pattern
The XRD section includes a **Load TiO2 rutile demo** button. This loads a bundled peak-picked pattern into the workflow and is useful for testing the interface without preparing your own diffraction file.

When using the demo pattern, enter **TiO2** as the composition.

### X-ray wavelength
The wavelength control is optional and is available under **Advanced XRD options** in the upload section. The default setting is **Cu Kα (1.5406 Å)**.

Use a different value when the uploaded diffraction file was collected with another source, for example:
- **Mo Kα**: `0.71073 Å`
- **Co Kα**: `1.78897 Å`
- a custom wavelength entered manually

## How generation works

1. Enter a composition and optional crystallographic constraints.
2. Optionally upload a peak-picked diffraction file.
3. The webapp stores uploaded diffraction files under `/app/data/uploads`.
4. A generation job is submitted to the CrystaLLM-pi API.
5. The API writes its output parquet file under `/app/outputs`.
6. The webapp reads the generated parquet, extracts the CIF, and displays the result.

## Verifying the setup

### 1. Confirm shared storage is visible from both hosts

On one host, create a test file:

```bash
echo "shared storage test" > /shared/crystallm/outputs/test.txt
```

On the other host, verify that the same file appears:

```bash
cat /shared/crystallm/outputs/test.txt
```

Do not continue until both hosts can see the same file.

### 2. Check API connectivity from the webapp container

```bash
docker compose exec -T webapp python - <<'PY'
import os, requests
url = os.environ["CRYSTALLM_PI_API_URL"].rstrip("/")
print("API:", url)
print("healthz:", requests.get(url + "/healthz", timeout=10).status_code)
PY
```

### 3. Check mounted shared paths inside the webapp container

```bash
docker compose exec -T webapp sh -lc 'ls -ld /app/data /app/data/uploads /app/outputs'
```

### 4. Launch the web interface

Open:

```text
http://localhost:8050
```

Submit a simple test job with:
- composition: `PbTe`
- no `Z`
- no space group
- no diffraction upload

If successful, the application should display a generated CIF and structure viewer.

## Logs

Follow webapp logs:

```bash
docker compose logs -f webapp
```

Follow all service logs:

```bash
docker compose logs -f
```

## Stop the stack

```bash
docker compose down
```

## Troubleshooting

### The webapp cannot reach the API
Check that:
- `CRYSTALLM_PI_API_URL` is correct
- the API service is running
- the API host is reachable from the webapp host
- firewalls allow traffic to port `8000`

Test directly from inside the webapp container:

```bash
docker compose exec -T webapp python - <<'PY'
import os, requests
url = os.environ["CRYSTALLM_PI_API_URL"].rstrip("/")
print(requests.get(url + "/healthz", timeout=10).text)
PY
```

### Diffraction-conditioned generation fails with missing file errors
Check that:
- the uploaded file exists under the shared `data/uploads` directory
- the API container mounts that same shared directory as `/app/data`
- the API can read the exact file path referenced in the job request

### Output parquet files are not found
Check that:
- the API is writing to `/app/outputs`
- the webapp container mounts the shared outputs directory as `/app/outputs`
- both hosts are using the same shared storage
- permissions allow both services to read and write

### The webapp starts but generation never finishes
This usually indicates one of:
- the API job failed
- the output parquet was written somewhere else
- the shared outputs directory is not visible to the webapp
- the API is unreachable after job submission

Inspect:
- API logs on the compute host
- webapp logs
- the shared outputs directory contents

## License

See the repository licence for details.
