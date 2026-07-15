# Remote conversion on `noesis`

Paper Pipeline can keep the dashboard, library, job queue, and artifact
installation on the local Windows machine while running only Marker conversion
on the Ubuntu GPU host reached as `ssh noesis`. Recipe calls and all library
writes remain local.

## 1. Prepare SSH

From the Windows machine, `ssh noesis` must resolve through the user's OpenSSH
configuration and authenticate without an interactive password prompt:

```powershell
ssh -o BatchMode=yes noesis nvidia-smi
```

The verified host is Ubuntu 24.04 with an RTX 3090. Paper Pipeline invokes the
system `ssh` and `scp` clients and deliberately uses `BatchMode=yes`, so an
unattended dashboard cannot stop at a password prompt.

## 2. Install the remote worker

The remote Python environment needs the same Paper Pipeline version as the
local machine plus the Marker extra. A fresh setup on `noesis` is:

```sh
ssh noesis
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/AssortedFantasy/paper-pipeline.git ~/apps/paper-pipeline
cd ~/apps/paper-pipeline
~/.local/bin/uv sync --extra marker --no-dev
nvidia-smi
.venv/bin/python -c "import marker, paper_pipeline, torch; print(torch.cuda.is_available())"
```

The final command must print `True`. The first real conversion may also
download Marker model weights, so the remote account needs outbound network
access and enough cache/disk space.

The currently verified `noesis` environment is
`/home/jehan/apps/marker/.venv-uv/bin/python`; it contains Marker 1.10.2 and
Paper Pipeline 2.0.0a0. Use an absolute interpreter path—`~` expansion and
shell activation are intentionally not part of the remote protocol.

## 3. Configure the local dashboard

Put these non-secret settings in `~/.paper-pipeline/.env` on Windows:

```dotenv
PAPER_PIPELINE_REMOTE_CONVERTER_HOST=noesis
PAPER_PIPELINE_REMOTE_CONVERTER_ROOT=/tmp/paper-pipeline
PAPER_PIPELINE_REMOTE_CONVERTER_PYTHON=/home/jehan/apps/marker/.venv-uv/bin/python
PAPER_PIPELINE_CONVERTER_TIMEOUT_SECONDS=1800
```

Then restart `uv run paper-pipeline serve`. When the host is set, the existing
Convert action automatically selects the SSH backend; there is no separate
remote queue or dashboard to start. Each attempt uploads one PDF into a random,
private directory below the configured remote root, downloads the validated
transcription and figures, and removes the remote attempt directory.

## 4. Verify before converting a library

The repository includes an explicit, environment-gated real-host check. From
PowerShell, point it at any real, non-sensitive PDF:

```powershell
$env:PAPER_PIPELINE_REMOTE_TEST = "1"
$env:PAPER_PIPELINE_REMOTE_TEST_PDF = "D:\Papers\test-paper.pdf"
uv run pytest -m gpu tests/convert/test_remote.py::test_real_remote_host_opt_in
```

This follows the production path: a fresh local conversion child, SSH/SCP,
the remote Marker worker, download validation, and local staging. It does not
install the result into a library. After it passes, start the local dashboard,
select one imported paper, choose **Convert selected**, and watch its live row
or the Jobs tab.

## Troubleshooting

- `SSH transport is unavailable`: ensure Windows OpenSSH provides both `ssh`
  and `scp` and that `ssh -o BatchMode=yes noesis true` succeeds.
- `remote conversion process failed`: run the remote Python import/CUDA check
  above; bare `/usr/bin/python3` is not sufficient on the verified host.
- Timeout during the first run: allow model downloads to finish, then retry or
  raise the local timeout.
- Version/import failures after a local update: update the remote checkout and
  rerun `uv sync --extra marker --no-dev` before retrying.

Remote stdout, administrator text, host paths, and credentials are never copied
into the library. Failed attempts retain only a safe local classification.
