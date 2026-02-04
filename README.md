# CrystaLLM-π Webapp (Docker API client)

This web UI calls the **CrystaLLM-π containerised API** (from https://github.com/C-Bone-UCL/CrystaLLM-pi) and displays **one generated CIF**.

It supports:
- Composition (reduced formula input like `PbTe` → converted to explicit `Pb1Te1`)
- Optional **Z** (formula units per cell) → multiplies the reduced formula by Z before sending
- Optional **space group**
- Optional **PXRD CSV** drag-and-drop (2 columns: 2θ 0–90, intensity 0–100)

## 1) Run the CrystaLLM-π API container

Follow the CrystaLLM-π README for building the container.

Important: mount THIS webapp’s `./data` and `./outputs` so the webapp can:
- write uploaded PXRD files under `./data/uploads`
- read generation output parquet under `./outputs`

Example (run from anywhere):

```bash
# inside CrystaLLM-pi repo:
docker build -t crystallm-api .

# from the webapp repo root:
mkdir -p data outputs

docker run \
  -u $(id -u):$(id -g) \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/outputs:/app/outputs \
  -e HF_KEY="your_hf_token_here" \
  -e WANDB_KEY="your_wandb_key_here" \
  --name crystallm-api \
  crystallm-api
