# TT Workshop PR Merger

Interactive script to review and rebase+merge Wokwi PRs on a TinyTapeout shuttle repository.

## Setup

```bash
pip install -r requirements.txt
```

Requires the [GitHub CLI (`gh`)](https://cli.github.com/) to be installed.

## Usage

```bash
python3 merge_prs.py <REPO> --token <GITHUB_TOKEN>
```

For example:

```bash
python3 merge_prs.py TinyTapeout/tinytapeout-sky-26a --token <GITHUB_TOKEN>
```

Or via environment variable:

```bash
GH_TOKEN=<GITHUB_TOKEN> python3 merge_prs.py TinyTapeout/tinytapeout-sky-26a
```

## Token Permissions

The GitHub personal access token needs the following permissions:

### Fine-grained PAT

- **Repository access**: The target repository
- **Contents**: Read and Write
- **Pull requests**: Read and Write
- **Metadata**: Read (granted automatically)

### Classic PAT

- **`repo`** scope (full control of private repositories)
