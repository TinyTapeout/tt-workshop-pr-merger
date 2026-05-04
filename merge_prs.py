#!/usr/bin/env python3
"""
Interactive script to review and rebase+merge project PRs
on a TinyTapeout shuttle repository.
"""

import argparse
import base64
import io
import json
import os
import re
import subprocess
import zipfile

import yaml

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"


def gh(*args, token=None, json_output=False, binary=False):
    cmd = ["gh"] + list(args)
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    result = subprocess.run(cmd, capture_output=True, text=not binary, env=env)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if binary else result.stderr
        print(f"  gh error: {stderr.strip()}")
        return None
    if binary:
        return result.stdout
    if json_output:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"  gh error: invalid JSON response")
            return None
    return result.stdout.strip()


def get_file_content(repo, path, ref, token=None):
    """Fetch a file from a specific branch via the GitHub API."""
    data = gh(
        "api",
        f"repos/{repo}/contents/{path}?ref={ref}",
        "--jq", ".content",
        token=token,
    )
    if not data:
        return None
    try:
        return base64.b64decode(data).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Warning: failed to decode {path}: {e}")
        return None


def get_upstream_run(repo, project_dir, branch, token=None):
    """Read commit_id.json for a project and parse the upstream workflow URL."""
    raw = get_file_content(repo, f"{project_dir}/commit_id.json", branch, token=token)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)", data.get("workflow_url") or "")
    if not m:
        return None
    return {"owner": m.group(1), "repo": m.group(2), "run_id": m.group(3)}


def fetch_metrics_csv(run_info, token=None):
    """Download tt_submission artifact for an upstream run and return parsed metrics.csv."""
    if not run_info:
        return None
    upstream = f"{run_info['owner']}/{run_info['repo']}"
    arts = gh(
        "api",
        f"repos/{upstream}/actions/runs/{run_info['run_id']}/artifacts",
        token=token,
        json_output=True,
    )
    if not arts:
        return None
    art_id = next((a["id"] for a in arts.get("artifacts", []) if a["name"] == "tt_submission"), None)
    if not art_id:
        return None
    data = gh("api", f"repos/{upstream}/actions/artifacts/{art_id}/zip", token=token, binary=True)
    if not data:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        with zf.open("tt_submission/stats/metrics.csv") as f:
            content = f.read().decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError):
        return None
    metrics = {}
    for line in content.splitlines()[1:]:
        if "," in line:
            k, _, v = line.partition(",")
            metrics[k.strip()] = v.strip()
    return metrics or None


def format_metrics(metrics):
    if not metrics:
        return []
    out = []
    util = metrics.get("design__instance__utilization")
    if util:
        try:
            out.append(f"Utilization:    {float(util) * 100:.2f}%")
        except ValueError:
            out.append(f"Utilization:    {util}")
    cells = metrics.get("design__instance__count")
    stdcells = metrics.get("design__instance__count__stdcell")
    if cells or stdcells:
        bits = []
        if cells:
            bits.append(f"total {cells}")
        if stdcells:
            bits.append(f"stdcell {stdcells}")
        out.append(f"Cells:          {', '.join(bits)}")
    return out


def is_wokwi(project_dir):
    return project_dir.startswith("projects/tt_um_wokwi_")


def main():
    parser = argparse.ArgumentParser(description="Review and rebase+merge TinyTapeout project PRs")
    parser.add_argument("repo", help="GitHub repository (e.g. TinyTapeout/tinytapeout-sky-26a)")
    parser.add_argument("--token", "-t", help="GitHub token (or set GH_TOKEN / GITHUB_TOKEN env var)")
    parser.add_argument(
        "--type",
        choices=["wokwi", "hdl", "all"],
        default="all",
        help="Which PRs to process: wokwi only, hdl only (non-wokwi), or all (default: all)",
    )
    args = parser.parse_args()

    repo = args.repo
    token = args.token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Warning: No GitHub token provided. Using existing gh auth.")
        print("  Pass --token TOKEN, or set GH_TOKEN / GITHUB_TOKEN env var.\n")

    print(f"Fetching open PRs from {repo}...")
    prs = gh(
        "pr", "list",
        "--repo", repo,
        "--limit", "500",
        "--json", "number,title,headRefName,body",
        token=token,
        json_output=True,
    )
    if not prs:
        print("No open PRs found.")
        return

    annotated = []
    for pr in prs:
        branch = pr["headRefName"]
        parts = branch.split("-")
        project_dir = "-".join(parts[:-1]) if len(parts) > 1 else branch
        if not project_dir.startswith("projects/tt_um_"):
            continue
        pr["_project_dir"] = project_dir
        pr["_is_wokwi"] = is_wokwi(project_dir)
        annotated.append(pr)

    if args.type == "wokwi":
        selected = [pr for pr in annotated if pr["_is_wokwi"]]
    elif args.type == "hdl":
        selected = [pr for pr in annotated if not pr["_is_wokwi"]]
    else:
        selected = annotated

    wokwi_count = sum(1 for pr in annotated if pr["_is_wokwi"])
    hdl_count = len(annotated) - wokwi_count
    print(f"Found {len(prs)} open PRs: {wokwi_count} wokwi, {hdl_count} hdl. Processing {len(selected)} (--type {args.type}).\n")

    # Sort by PR number (ascending)
    selected.sort(key=lambda pr: pr["number"])

    merged = 0
    skipped = 0

    for i, pr in enumerate(selected, 1):
        # Clear screen and scrollback buffer
        print("\033[3J\033[2J\033[H", end="", flush=True)

        num = pr["number"]
        branch = pr["headRefName"]
        project_dir = pr["_project_dir"]
        is_wokwi_pr = pr["_is_wokwi"]

        print("=" * 70)
        print(f"[{i}/{len(selected)}] PR #{num}: {pr['title']}  ({'wokwi' if is_wokwi_pr else 'hdl'})")
        print(f"  Branch: {branch}")
        print(f"  {pr.get('body', '').split('\n')[0]}")
        print()

        # Check CI status
        checks = gh(
            "pr", "checks", str(num),
            "--repo", repo,
            "--json", "name,state",
            token=token,
            json_output=True,
        )
        if checks:
            all_success = all(c["state"] == "SUCCESS" for c in checks)
            if all_success:
                print(f"  {GREEN}Checks: all passed{RESET}")
            else:
                for c in checks:
                    if c["state"] != "SUCCESS":
                        print(f"  {RED}WARNING: {c['name']} -> {c['state']}{RESET}")
        else:
            print(f"  {RED}WARNING: Could not fetch checks{RESET}")
        print()

        # Fetch and display key fields from info.yaml
        info_yaml_raw = get_file_content(repo, f"{project_dir}/info.yaml", branch, token=token)
        if info_yaml_raw:
            try:
                info = yaml.safe_load(info_yaml_raw)
                proj = info.get("project", {})
                pinout = info.get("pinout", {})

                print("--- info.yaml ---")
                print(f"  Title:       {proj.get('title', '?')}")
                print(f"  Author:      {proj.get('author', '?')}")
                print(f"  Discord:     {proj.get('discord', '') or '(none)'}")
                print(f"  Description: {proj.get('description', '?')}")

                if not is_wokwi_pr:
                    print(f"  Top module:  {proj.get('top_module', '?')}")
                    src_files = proj.get("source_files") or []
                    if src_files:
                        print(f"  Sources:     {', '.join(src_files)}")
                    else:
                        print("  Sources:     (none listed)")
                    tiles = proj.get("tiles")
                    if tiles:
                        print(f"  Tiles:       {tiles}")

                # Condensed pinout: group by type (ui, uo, uio), one line each
                used = {k: v for k, v in pinout.items() if v and v.strip() and v.strip() != '""'}
                if used:
                    groups = {"ui": [], "uo": [], "uio": []}
                    for k, v in used.items():
                        for prefix in ("uio", "uo", "ui"):
                            if k.startswith(prefix):
                                groups[prefix].append(f"{k}={v}")
                                break
                    for prefix, label in [("ui", "Inputs"), ("uo", "Outputs"), ("uio", "Bidir")]:
                        if groups[prefix]:
                            print(f"  {label:10s}   {', '.join(groups[prefix])}")
                else:
                    print("  Pinout:      (all blank)")
                print()
            except yaml.YAMLError:
                print("--- info.yaml (parse error, showing raw) ---")
                print(info_yaml_raw[:2000])
                print()

        # Fetch and display info.md (strip boilerplate comment)
        info_md = get_file_content(repo, f"{project_dir}/docs/info.md", branch, token=token)
        if info_md:
            info_md = re.sub(r'<!---?\s*\n.*?-->\n*', '', info_md, flags=re.DOTALL)
        if info_md and info_md.strip():
            print("--- docs/info.md ---")
            print(info_md[:2000])
            print()

        if not is_wokwi_pr:
            run_info = get_upstream_run(repo, project_dir, branch, token=token)
            metrics = fetch_metrics_csv(run_info, token=token)
            formatted = format_metrics(metrics)
            if formatted:
                print("--- workflow metrics ---")
                for line in formatted:
                    print(f"  {line}")
                print()
            elif run_info:
                print(f"  (workflow metrics unavailable: {run_info['owner']}/{run_info['repo']} run {run_info['run_id']})\n")

        # Show the command and ask user
        print(f"Command: gh pr merge {num} --repo {repo} --rebase")
        while True:
            answer = input(f"Rebase+merge PR #{num}? [y/n/q] ").strip().lower()
            if answer in ("y", "n", "q"):
                break
            print("  Please enter y, n, or q (quit).")

        if answer == "q":
            print("Quitting.")
            break
        elif answer == "n":
            skipped += 1
            print(f"  Skipped PR #{num}\n")
            continue

        # Merge
        print(f"  Merging PR #{num}...")
        result = gh(
            "pr", "merge", str(num),
            "--repo", repo,
            "--rebase",
            token=token,
        )
        if result is not None:
            print(f"  Merged PR #{num}")
            merged += 1
        else:
            print(f"  Failed to merge PR #{num}")
            skipped += 1
        print()

    print("=" * 70)
    print(f"Done. Merged: {merged}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
