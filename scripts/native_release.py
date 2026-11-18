#!/usr/bin/env python3
"""Controlled-fork native handoff using standard GitHub draft-release APIs.

The founder's authenticated asset upload is the trust boundary. Runtime facts
are captured by this helper, not independently attested by GitHub.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

REPO = "DreamwareDevelopment/codex-claude-transfer"
DEFAULT = "Main"
WORKFLOW = ".github/workflows/release.yml"
OPERATOR = 25471746
PREFIX = "CCT_NATIVE_HANDOFF_V1\n"
RECEIPT = "native-darwin-arm64.json"
LOG = "native-darwin-arm64.log"
ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("linux_amd64", "linux_arm64", "darwin_amd64", "darwin_arm64", "windows_amd64")
ARTIFACTS = tuple("cct_" + target for target in TARGETS) + ("codex-claude-transfer-go-dependencies",)
JOBS = ("Resolve version", "Package Go dependencies") + tuple("Build " + t.replace("_", "/") for t in TARGETS) + (
    "Smoke-test linux/amd64 artifact", "Smoke-test windows/amd64 artifact")


class Refused(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def run(argv, *, cwd=ROOT, log=None, env=None):
    subprocess.run(argv, cwd=cwd, check=True, stdout=log, stderr=subprocess.STDOUT if log else None, env=env)


def output(argv):
    return subprocess.check_output(argv, cwd=ROOT, text=True).strip()


def api(path):
    return json.loads(output(["gh", "api", path]))


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clean_source():
    require(not output(["git", "status", "--porcelain"]), "source checkout must be clean")
    return output(["git", "rev-parse", "HEAD"])


def native_runtime():
    require(platform.system() == "Darwin", "actual Darwin executor required")
    require(platform.machine() == "arm64", "actual Darwin arm64 executor required")
    require(os.environ.get("DREAMWARE_TEST_ACTIVE") == "1", "run the entire native command through dreamware-test")
    return {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version(),
            "python_path": str(Path(sys.executable).resolve()), "python_sha256": digest(Path(sys.executable).resolve()),
            "go": output(["go", "version"]), "go_path": str(Path(shutil.which("go")).resolve()),
            "go_sha256": digest(Path(shutil.which("go")).resolve())}


def native_ci(log):
    runtime = native_runtime()
    env = dict(os.environ, GOMAXPROCS="1")
    for command in (["go", "build", "-p", "1", "./..."], ["go", "vet", "-p", "1", "./..."],
                    ["go", "test", "-p", "1", "./..."]):
        log.write(("COMMAND " + " ".join(command) + "\n").encode())
        log.flush()
        run(command, log=log, env=env)
    return runtime


def validate_producer(meta, jobs, artifacts, run_id, attempt, preparing=False):
    require(type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0, "invalid producer identity")
    require(meta.get("id") == run_id and meta.get("run_attempt") == attempt, "producer attempt changed")
    require(meta.get("repository", {}).get("full_name") == REPO and
            meta.get("head_repository", {}).get("full_name") == REPO, "wrong producer repository")
    require(meta.get("path") == WORKFLOW, "wrong producer workflow")
    require(type(meta.get("workflow_id")) is int and meta["workflow_id"] > 0, "invalid producer workflow identity")
    require(meta.get("event") in ("push", "workflow_dispatch"), "untrusted producer event")
    require(re.fullmatch(r"[0-9a-f]{40}", meta.get("head_sha", "")), "invalid producer source")
    if preparing:
        require(meta.get("status") == "in_progress", "draft preparation must run inside the current producer")
    else:
        require(meta.get("status") == "completed" and meta.get("conclusion") == "success", "producer did not succeed")
    for name in JOBS:
        matching = [job for job in jobs if job.get("name") == name]
        require(len(matching) == 1 and matching[0].get("status") == "completed" and
                matching[0].get("conclusion") == "success", "missing, ambiguous or unsuccessful job: " + name)
    require(len(artifacts) == len(ARTIFACTS) and {a.get("name") for a in artifacts} == set(ARTIFACTS),
            "unexpected or ambiguous producer artifact inventory")
    for artifact in artifacts:
        require(type(artifact.get("id")) is int and artifact["id"] > 0 and not artifact.get("expired", True),
                "invalid or expired artifact")
        require(isinstance(artifact.get("digest"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]), "artifact has no server digest")
        require(artifact.get("workflow_run", {}).get("id") == run_id and
                artifact.get("workflow_run", {}).get("head_sha") == meta["head_sha"], "artifact source mismatch")
    return {"repo": REPO, "run_id": run_id, "attempt": attempt, "source_sha": meta["head_sha"],
            "workflow_id": meta["workflow_id"], "workflow_path": WORKFLOW,
            "event": meta["event"], "head_branch": meta["head_branch"],
            "artifacts": sorted(({key: a[key] for key in ("id", "name", "digest")} for a in artifacts), key=lambda a: a["name"])}


def producer(run_id, attempt, preparing=False):
    base = f"repos/{REPO}/actions/runs/{run_id}"
    meta = api(base)
    jobs_page = api(base + f"/attempts/{attempt}/jobs?per_page=100")
    artifacts_page = api(base + "/artifacts?per_page=100")
    require(jobs_page["total_count"] <= 100 and artifacts_page["total_count"] <= 100, "unbounded producer inventory")
    result = validate_producer(meta, jobs_page["jobs"], artifacts_page["artifacts"], run_id, attempt, preparing)
    workflow = api(f"repos/{REPO}/contents/{WORKFLOW}?ref={result['source_sha']}")
    require(workflow.get("type") == "file", "producer workflow is not a committed file")
    result["workflow_blob"] = workflow["sha"]
    current = api(f"repos/{REPO}/commits/{DEFAULT}")["sha"]
    require(current == result["source_sha"], "producer source is no longer current default source")
    return result


def version_for(binding):
    if binding["event"] == "push":
        tag = binding["head_branch"]
        require(re.fullmatch(r"v[0-9][A-Za-z0-9._+-]*", tag), "unsupported release tag")
        return tag, True
    return "v0.0.0-dryrun-" + binding["source_sha"][:7], False


def validate_draft(draft, binding):
    require(draft.get("draft") is True and draft.get("target_commitish") == binding["source_sha"], "wrong or published draft")
    body = draft.get("body", "")
    require(body.startswith(PREFIX) and len(body) <= 16384, "missing bounded draft binding")
    record = json.loads(body[len(PREFIX):])
    version, is_release = version_for(binding)
    require(record.get("schema_version") == 1 and record.get("binding") == binding and
            record.get("version") == version and record.get("is_release") is is_release and
            type(record.get("sign")) is bool, "draft producer binding mismatch")
    expected_tag = version if is_release else f"native-handoff-{binding['run_id']}-{binding['attempt']}"
    require(draft.get("tag_name") == expected_tag, "draft tag mismatch")
    return record


def one_asset(draft, name, founder=False):
    matches = [asset for asset in draft.get("assets", []) if asset.get("name") == name]
    require(len(matches) == 1, "missing or ambiguous native asset: " + name)
    asset = matches[0]
    require(type(asset.get("id")) is int and asset["id"] > 0 and asset.get("state") == "uploaded", "native asset incomplete")
    require(type(asset.get("size")) is int and 0 < asset["size"] <= 16 * 1024 * 1024, "native evidence size out of bounds")
    if founder:
        require(asset.get("uploader", {}).get("id") == OPERATOR, "native evidence lacks authenticated founder custody")
    return asset


def download(path, destination, max_size):
    with open(destination, "wb") as stream:
        subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream", path], cwd=ROOT, stdout=stream, check=True)
    require(0 < destination.stat().st_size <= max_size, "download size out of bounds")


def download_asset(asset, destination):
    download(f"repos/{REPO}/releases/assets/{asset['id']}", destination, 16 * 1024 * 1024)
    require(destination.stat().st_size == asset["size"], "native evidence size changed")
    server_digest = asset.get("digest")
    require(server_digest == "sha256:" + digest(destination), "native evidence digest mismatch")


def download_artifact(artifact, directory):
    archive_zip = directory / (str(artifact["id"]) + ".zip")
    download(f"repos/{REPO}/actions/artifacts/{artifact['id']}/zip", archive_zip, 2 * 1024 ** 3)
    require("sha256:" + digest(archive_zip) == artifact["digest"], "Actions artifact bytes changed")
    with zipfile.ZipFile(archive_zip) as archive:
        files = archive.infolist()
        require(len(files) == 1 and files[0].filename == Path(files[0].filename).name and
                files[0].file_size <= 2 * 1024 ** 3 and not files[0].is_dir(), "unsafe Actions artifact layout")
        name = files[0].filename
        require(re.fullmatch(r"[A-Za-z0-9._+-]+\.(tar\.gz|tar\.xz)", name), "unexpected artifact archive name")
        destination = directory / name
        require(not destination.exists(), "duplicate downloaded archive")
        with archive.open(files[0]) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    archive_zip.unlink()
    return destination


def packaged_smoke(archive, version, log):
    require(archive.name == f"cct_{version}_darwin_arm64.tar.gz", "wrong native archive name")
    before = digest(archive)
    with tempfile.TemporaryDirectory(prefix="cct-native-smoke-") as scratch:
        directory = Path(scratch)
        top = f"cct_{version}_darwin_arm64"
        with tarfile.open(archive, "r:gz") as package:
            members = package.getmembers()
            require({member.name.rstrip("/") for member in members} == {top, top + "/cct", top + "/README.md", top + "/safety.md"},
                    "unexpected native archive inventory")
            for member in members:
                require(member.isdir() or member.isfile(), "native archive contains a link or special file")
                require(not member.mode & 0o022, "writable native archive entry")
            binary = next(member for member in members if member.name == top + "/cct")
            require(binary.mode & 0o777 == 0o755, "native packaged binary mode must be 0755")
            package.extractall(directory, filter="data")
        binary_path = directory / top / "cct"
        with binary_path.open("rb") as stream:
            require(stream.read(8) == b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01", "packaged binary must be thin arm64 Mach-O")
        run(["bash", str(ROOT / "scripts/smoke-artifact.sh"), str(binary_path)], cwd=directory, log=log)
    require(digest(archive) == before, "native archive changed during smoke")
    return before


def validate_receipt(receipt, binding, draft_id, log_asset, log_hash):
    require(receipt.get("schema_version") == 1 and receipt.get("policy") == "cct-native-release-v1", "wrong native receipt policy")
    require(receipt.get("binding") == binding and receipt.get("draft_id") == draft_id, "stale native receipt binding")
    require(receipt.get("status") == "success" and receipt.get("commands") == ["go build -p 1 ./...", "go vet -p 1 ./...", "go test -p 1 ./...", "bash scripts/smoke-artifact.sh <packaged-darwin-arm64>"], "native workload did not complete")
    runtime = receipt.get("runtime", {})
    require(set(runtime) == {"system", "machine", "python", "python_path", "python_sha256", "go", "go_path", "go_sha256"} and
            all(isinstance(value, str) and value for value in runtime.values()) and
            runtime.get("system") == "Darwin" and runtime.get("machine") == "arm64" and
            runtime == receipt.get("runtime_after"), "native runtime changed or missing")
    require(receipt.get("log") == {"id": log_asset["id"], "sha256": log_hash}, "native log custody mismatch")
    require(re.fullmatch(r"[0-9a-f]{64}", receipt.get("archive_sha256", "")), "missing native archive digest")
    artifact = next(a for a in binding["artifacts"] if a["name"] == "cct_darwin_arm64")
    require(receipt.get("artifact") == artifact, "native artifact identity mismatch")


def github_outputs(values):
    path = os.environ.get("GITHUB_OUTPUT")
    require(path is not None, "GitHub output file missing")
    with open(path, "a") as stream:
        for key, value in values.items():
            stream.write(f"{key}={str(value).lower() if type(value) is bool else value}\n")


def prepare(args):
    binding = producer(args.run, args.attempt, preparing=True)
    require(clean_source() == binding["source_sha"], "producer checkout mismatch")
    version, is_release = version_for(binding)
    record = {"schema_version": 1, "binding": binding, "version": version, "is_release": is_release, "sign": args.sign}
    tag = version if is_release else f"native-handoff-{args.run}-{args.attempt}"
    payload = {"tag_name": tag, "target_commitish": binding["source_sha"], "name": version + " (native proof pending)",
               "body": PREFIX + json.dumps(record, sort_keys=True), "draft": True}
    draft = json.loads(subprocess.check_output(["gh", "api", "--method", "POST", f"repos/{REPO}/releases", "--input", "-"],
                                               input=json.dumps(payload), text=True, cwd=ROOT))
    github_outputs({"draft_id": draft["id"]})
    print(f"Native handoff draft {draft['id']} prepared; assembly/publication remain blocked.")


def native(args):
    source = clean_source()
    binding = producer(args.run, args.attempt)
    require(source == binding["source_sha"], "native source is not exact producer source")
    draft = api(f"repos/{REPO}/releases/{args.draft}")
    record = validate_draft(draft, binding)
    require(api("user")["id"] == OPERATOR, "founder authenticated operator required")
    require(not any(a.get("name") in (LOG, RECEIPT) for a in draft["assets"]), "native handoff already exists; do not clobber")
    args.output.mkdir(parents=True, exist_ok=False)
    artifact = next(a for a in binding["artifacts"] if a["name"] == "cct_darwin_arm64")
    archive = download_artifact(artifact, args.output)
    log_path = args.output / LOG
    with log_path.open("wb") as log:
        runtime = native_ci(log)
        archive_hash = packaged_smoke(archive, record["version"], log)
    require(clean_source() == source and producer(args.run, args.attempt) == binding, "source/producer changed during native work")
    runtime_after = native_runtime()
    require(runtime == runtime_after, "native runtime changed")
    run(["gh", "release", "upload", draft["tag_name"], str(log_path), "--repo", REPO])
    draft_after = api(f"repos/{REPO}/releases/{args.draft}")
    require(validate_draft(draft_after, binding) == record, "draft changed during native work")
    log_asset = one_asset(draft_after, LOG, founder=True)
    require(log_asset.get("digest") == "sha256:" + digest(log_path), "uploaded native log changed")
    receipt = {"schema_version": 1, "policy": "cct-native-release-v1", "binding": binding, "draft_id": args.draft,
               "runtime": runtime, "runtime_after": runtime_after, "status": "success", "artifact": artifact,
               "archive_sha256": archive_hash, "log": {"id": log_asset["id"], "sha256": digest(log_path)},
               "commands": ["go build -p 1 ./...", "go vet -p 1 ./...", "go test -p 1 ./...", "bash scripts/smoke-artifact.sh <packaged-darwin-arm64>"]}
    receipt_path = args.output / RECEIPT
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    run(["gh", "release", "upload", draft["tag_name"], str(receipt_path), "--repo", REPO])
    if args.dispatch:
        run(["gh", "workflow", "run", "release.yml", "--repo", REPO, "--ref", DEFAULT,
             "-f", f"producer_run={args.run}", "-f", f"producer_attempt={args.attempt}", "-f", f"draft_id={args.draft}"])
    print("Native operator receipt uploaded. Linux continuation retains assembly/signing/publication authority.")


def verify(args):
    binding = producer(args.run, args.attempt)
    require(clean_source() == binding["source_sha"], "continuation source must equal current producer/default source")
    draft = api(f"repos/{REPO}/releases/{args.draft}")
    record = validate_draft(draft, binding)
    receipt_asset, log_asset = one_asset(draft, RECEIPT, True), one_asset(draft, LOG, True)
    args.output.mkdir(parents=True, exist_ok=False)
    receipt_path, log_path = args.output / RECEIPT, args.output / LOG
    download_asset(receipt_asset, receipt_path)
    download_asset(log_asset, log_path)
    receipt = json.loads(receipt_path.read_text())
    validate_receipt(receipt, binding, args.draft, log_asset, digest(log_path))
    for artifact in binding["artifacts"]:
        archive = download_artifact(artifact, args.output)
        if args.compare is not None:
            require((args.compare / archive.name).is_file() and digest(args.compare / archive.name) == digest(archive),
                    "assembled archive differs from original producer bytes")
        if artifact["name"] == "cct_darwin_arm64":
            require(digest(archive) == receipt["archive_sha256"], "native proof does not cover assembled archive bytes")
    final_draft = api(f"repos/{REPO}/releases/{args.draft}")
    require(validate_draft(final_draft, binding) == record and
            one_asset(final_draft, RECEIPT, True) == receipt_asset and one_asset(final_draft, LOG, True) == log_asset and
            producer(args.run, args.attempt) == binding, "native custody or producer raced")
    github_outputs({"version": record["version"], "is_release": record["is_release"], "sign": record["sign"], "draft_id": args.draft})
    print("Verified authenticated operator proof and exact original producer archives.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "native", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--run", type=int, required=True)
        command.add_argument("--attempt", type=int, required=True)
        if name == "prepare":
            command.add_argument("--sign", action="store_true")
        else:
            command.add_argument("--draft", type=int, required=True)
            command.add_argument("--output", type=Path, required=True)
        if name == "native":
            command.add_argument("--dispatch", action="store_true", help="dispatch continuation, which can publish a real tagged release")
        if name == "verify":
            command.add_argument("--compare", type=Path, help="recheck assembled files before publication")
    ci = commands.add_parser("ci")
    ci.add_argument("--log", type=Path, required=True)
    smoke = commands.add_parser("smoke-local")
    smoke.add_argument("--archive", type=Path, required=True)
    smoke.add_argument("--version", required=True)
    smoke.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    if args.command in ("ci", "smoke-local"):
        source = clean_source()
        with args.log.open("xb") as log:
            runtime = native_ci(log) if args.command == "ci" else native_runtime()
            if args.command == "smoke-local":
                packaged_smoke(args.archive.resolve(), args.version, log)
        require(clean_source() == source and native_runtime() == runtime, "source/runtime changed")
        print(json.dumps({"source_sha": source, "runtime": runtime, "status": "success", "log_sha256": digest(args.log)}))
    else:
        {"prepare": prepare, "native": native, "verify": verify}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (Refused, subprocess.CalledProcessError, OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
        print("Native release refused: " + str(error), file=sys.stderr)
        sys.exit(1)
